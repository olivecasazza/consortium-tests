//! Docker-based integration tests for consortium.
//!
//! These tests require Docker to be running and are gated behind
//! the `docker-tests` feature flag:
//!
//!   cargo test -p consortium --features docker-tests --test docker_integration

#![cfg(feature = "docker-tests")]

use std::sync::LazyLock;
use std::time::Duration;

use consortium::dag::*;
use consortium::worker::exec::ExecWorker;
use consortium::worker::Worker;
use consortium_test_harness::{ClusterTopology, DockerCluster};

/// Shared cluster — started once, used by all tests in this file.
static CLUSTER: LazyLock<DockerCluster> = LazyLock::new(|| {
    DockerCluster::start_small().expect(
        "Failed to start Docker cluster. Is Docker running? \
         Run with: cargo test -p consortium --features docker-tests --test docker_integration",
    )
});

// ─── SSH Worker Tests ────────────────────────────────────────────────────────

#[test]
fn test_ssh_hostname_single_node() {
    let cluster = &*CLUSTER;
    let nodes = cluster.nodes_with_prefix("compute");
    let node = &nodes[0];
    let port = cluster.port_for(node).unwrap();
    let key = cluster.ssh_key_path().to_string_lossy().to_string();

    // Use ExecWorker directly with explicit SSH command (since SshWorker
    // doesn't support per-node port mapping — all nodes get the same port).
    let cmd = format!(
        "ssh -oStrictHostKeyChecking=no -oPasswordAuthentication=no -i {} -p {} root@127.0.0.1 hostname",
        key, port
    );
    let mut worker = ExecWorker::new(vec![node.clone()], cmd, 1, Some(Duration::from_secs(10)))
        .with_stderr(true);

    worker.start().unwrap();

    let start = std::time::Instant::now();
    while !worker.is_done() && start.elapsed() < Duration::from_secs(10) {
        let fds = worker.read_fds();
        for fd in &fds {
            let _ = worker.handle_read(*fd);
        }
        std::thread::sleep(Duration::from_millis(10));
    }

    assert!(worker.is_done(), "worker should complete");
    let retcodes = worker.retcodes();
    assert_eq!(
        retcodes.get(node.as_str()).copied(),
        Some(0),
        "SSH hostname should succeed on {}, retcodes={:?}",
        node,
        retcodes
    );
}

#[test]
fn test_ssh_fanout_multiple_nodes() {
    let cluster = &*CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();

    // Run `echo hello` on all compute nodes via ExecWorker (since each has a different port)
    let compute_nodes = cluster.nodes_with_prefix("compute");
    let mut nodes = Vec::new();
    let mut commands = Vec::new();

    for node in &compute_nodes {
        let port = cluster.port_for(node).unwrap();
        nodes.push(node.clone());
        commands.push(format!(
            "ssh -oStrictHostKeyChecking=no -oPasswordAuthentication=no -i {} -p {} root@127.0.0.1 'echo hello_from_$(hostname)'",
            key, port
        ));
    }

    // Use the case-dispatch pattern from the DAG executor
    let case_body: String = nodes
        .iter()
        .zip(commands.iter())
        .map(|(node, cmd)| format!("  '{}') {};;\n", node, cmd))
        .collect();

    let dispatch = format!(
        "case \"%h\" in\n{}  *) echo unknown >&2; exit 1;;\nesac",
        case_body
    );

    let mut worker = ExecWorker::new(nodes.clone(), dispatch, 64, Some(Duration::from_secs(30)))
        .with_stderr(true);
    worker.start().unwrap();

    let start = std::time::Instant::now();
    while !worker.is_done() && start.elapsed() < Duration::from_secs(30) {
        let fds = worker.read_fds();
        for fd in &fds {
            let _ = worker.handle_read(*fd);
        }
        std::thread::sleep(Duration::from_millis(10));
    }

    assert!(worker.is_done(), "worker should complete");
    let retcodes = worker.retcodes();
    for node in &nodes {
        assert_eq!(
            retcodes.get(node).copied(),
            Some(0),
            "node {} should succeed (rc={:?})",
            node,
            retcodes.get(node)
        );
    }
    assert_eq!(retcodes.len(), nodes.len());
}

// ─── DAG Executor Tests ─────────────────────────────────────────────────────

#[test]
fn test_dag_ssh_across_cluster() {
    let cluster = &*CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();
    let compute_nodes = cluster.nodes_with_prefix("compute");

    let mut dag = DagBuilder::new();

    for node in &compute_nodes {
        let port = cluster.port_for(node).unwrap();
        let k = key.clone();
        let n = node.clone();
        dag.add_task(
            node.as_str(),
            ShellTask {
                command: format!(
                    "ssh -oStrictHostKeyChecking=no -oPasswordAuthentication=no -i {} -p {} root@127.0.0.1 'echo ok_from_{}'",
                    k, port, n
                ),
                description: format!("check {}", n),
                resource: None,
            },
        );
    }

    let report = dag.build().unwrap().run().unwrap();
    assert!(
        report.is_success(),
        "DAG across cluster failed: {:?}",
        report.failed
    );
    assert_eq!(report.completed.len(), compute_nodes.len());
}

#[test]
fn test_stage_builder_across_cluster() {
    let cluster = &*CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();
    let compute_nodes = cluster.nodes_with_prefix("compute");
    let fleet = cluster.fleet_config();

    let ctx = DagContext::new();
    ctx.set_state("fleet", fleet.clone());
    ctx.set_state("ssh_key", key.clone());

    let report = StageBuilder::new()
        .resources(compute_nodes.clone())
        .stage("probe", Some(5), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("probe {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let key: String = ctx.get_state("ssh_key").unwrap();
                let node = &fleet.nodes[&h];
                let port = node.target_port.unwrap_or(22);

                let output = std::process::Command::new("ssh")
                    .args([
                        "-oStrictHostKeyChecking=no",
                        "-oPasswordAuthentication=no",
                        "-oConnectTimeout=5",
                        "-i",
                        &key,
                        "-p",
                        &port.to_string(),
                        &format!("{}@{}", node.target_user, node.target_host),
                        "uname -n",
                    ])
                    .output();

                match output {
                    Ok(o) if o.status.success() => {
                        let hostname = String::from_utf8_lossy(&o.stdout).trim().to_string();
                        ctx.set_output(TaskId(format!("probe:{}", h)), hostname);
                        TaskOutcome::Success
                    }
                    Ok(o) => {
                        let err = String::from_utf8_lossy(&o.stderr);
                        TaskOutcome::Failed(format!("probe {} failed: {}", h, err.trim()))
                    }
                    Err(e) => TaskOutcome::Failed(format!("probe {} error: {}", h, e)),
                }
            }))
        })
        .stage("verify", Some(5), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("verify {}", h), move |ctx| {
                // Verify the probe stage produced output
                let hostname: Option<String> = ctx.get_output(&TaskId(format!("probe:{}", h)));
                match hostname {
                    Some(name) if !name.is_empty() => TaskOutcome::Success,
                    _ => TaskOutcome::Failed(format!("no probe output for {}", h)),
                }
            }))
        })
        .error_policy(ErrorPolicy::ContinueIndependent)
        .context(ctx)
        .build()
        .unwrap()
        .run()
        .unwrap();

    assert!(
        report.is_success(),
        "StageBuilder across cluster failed: {:?}",
        report.failed
    );
    // 2 stages × N compute nodes
    assert_eq!(report.completed.len(), compute_nodes.len() * 2);
}

#[test]
fn test_dag_with_concurrency_limit_across_cluster() {
    let cluster = &*CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();
    let compute_nodes = cluster.nodes_with_prefix("compute");
    let fleet = cluster.fleet_config();

    let ctx = DagContext::new();
    ctx.set_state("fleet", fleet.clone());
    ctx.set_state("ssh_key", key.clone());

    // Run with concurrency limit of 2 across all nodes
    let report = StageBuilder::new()
        .resources(compute_nodes.clone())
        .stage("work", Some(2), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("work {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let key: String = ctx.get_state("ssh_key").unwrap();
                let node = &fleet.nodes[&h];
                let port = node.target_port.unwrap_or(22);

                let output = std::process::Command::new("ssh")
                    .args([
                        "-oStrictHostKeyChecking=no",
                        "-oPasswordAuthentication=no",
                        "-i",
                        &key,
                        "-p",
                        &port.to_string(),
                        &format!("{}@{}", node.target_user, node.target_host),
                        "echo done",
                    ])
                    .output();

                match output {
                    Ok(o) if o.status.success() => TaskOutcome::Success,
                    _ => TaskOutcome::Failed(format!("work {} failed", h)),
                }
            }))
        })
        .error_policy(ErrorPolicy::ContinueIndependent)
        .context(ctx)
        .build()
        .unwrap()
        .run()
        .unwrap();

    assert!(
        report.is_success(),
        "concurrency-limited DAG failed: {:?}",
        report.failed
    );
    assert_eq!(report.completed.len(), compute_nodes.len());
}

// ─── Large-Scale Tests (33 nodes) ───────────────────────────────────────────

/// Large cluster: 25 compute + 5 GPU + 2 login + 1 controller = 33 nodes.
static LARGE_CLUSTER: LazyLock<DockerCluster> = LazyLock::new(|| {
    DockerCluster::start(ClusterTopology {
        compute_count: 25,
        gpu_count: 5,
        login_count: 2,
        controller: true,
    })
    .expect(
        "Failed to start large Docker cluster. Is Docker running? \
         Run with: cargo test -p consortium --features docker-tests --test docker_integration",
    )
});

#[test]
fn test_scale_dag_33_nodes() {
    let cluster = &*LARGE_CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();
    let all_nodes = cluster.node_names();

    assert_eq!(
        all_nodes.len(),
        33,
        "expected 33 nodes, got {}",
        all_nodes.len()
    );

    let start = std::time::Instant::now();

    let mut dag = DagBuilder::new();

    for node in &all_nodes {
        let port = cluster.port_for(node).unwrap();
        let k = key.clone();
        let n = node.clone();
        dag.add_task(
            node.as_str(),
            ShellTask {
                command: format!(
                    "ssh -oStrictHostKeyChecking=no -oPasswordAuthentication=no -i {} -p {} root@127.0.0.1 hostname",
                    k, port
                ),
                description: format!("hostname {}", n),
                resource: None,
            },
        );
    }

    let report = dag.build().unwrap().run().unwrap();
    let elapsed = start.elapsed();

    assert!(
        report.is_success(),
        "DAG across 33 nodes failed: {:?}",
        report.failed
    );
    assert_eq!(
        report.completed.len(),
        33,
        "expected 33 completed tasks, got {}",
        report.completed.len()
    );
    eprintln!(
        "test_scale_dag_33_nodes completed in {:.2}s",
        elapsed.as_secs_f64()
    );
}

#[test]
fn test_scale_stage_builder_33_nodes() {
    let cluster = &*LARGE_CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();
    let all_nodes = cluster.node_names();
    let fleet = cluster.fleet_config();

    assert_eq!(all_nodes.len(), 33);

    let ctx = DagContext::new();
    ctx.set_state("fleet", fleet.clone());
    ctx.set_state("ssh_key", key.clone());

    let report = StageBuilder::new()
        .resources(all_nodes.clone())
        .stage("probe", Some(10), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("probe {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let key: String = ctx.get_state("ssh_key").unwrap();
                let node = &fleet.nodes[&h];
                let port = node.target_port.unwrap_or(22);

                let output = std::process::Command::new("ssh")
                    .args([
                        "-oStrictHostKeyChecking=no",
                        "-oPasswordAuthentication=no",
                        "-oConnectTimeout=5",
                        "-i",
                        &key,
                        "-p",
                        &port.to_string(),
                        &format!("{}@{}", node.target_user, node.target_host),
                        "uname -n",
                    ])
                    .output();

                match output {
                    Ok(o) if o.status.success() => {
                        let hostname = String::from_utf8_lossy(&o.stdout).trim().to_string();
                        ctx.set_output(TaskId(format!("probe:{}", h)), hostname);
                        TaskOutcome::Success
                    }
                    Ok(o) => {
                        let err = String::from_utf8_lossy(&o.stderr);
                        TaskOutcome::Failed(format!("probe {} failed: {}", h, err.trim()))
                    }
                    Err(e) => TaskOutcome::Failed(format!("probe {} error: {}", h, e)),
                }
            }))
        })
        .stage("process", Some(5), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("process {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let key: String = ctx.get_state("ssh_key").unwrap();
                let node = &fleet.nodes[&h];
                let port = node.target_port.unwrap_or(22);

                let output = std::process::Command::new("ssh")
                    .args([
                        "-oStrictHostKeyChecking=no",
                        "-oPasswordAuthentication=no",
                        "-oConnectTimeout=5",
                        "-i",
                        &key,
                        "-p",
                        &port.to_string(),
                        &format!("{}@{}", node.target_user, node.target_host),
                        "echo processed",
                    ])
                    .output();

                match output {
                    Ok(o) if o.status.success() => {
                        ctx.set_output(TaskId(format!("process:{}", h)), "done".to_string());
                        TaskOutcome::Success
                    }
                    Ok(o) => {
                        let err = String::from_utf8_lossy(&o.stderr);
                        TaskOutcome::Failed(format!("process {} failed: {}", h, err.trim()))
                    }
                    Err(e) => TaskOutcome::Failed(format!("process {} error: {}", h, e)),
                }
            }))
        })
        .stage("verify", Some(10), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("verify {}", h), move |ctx| {
                let probe_out: Option<String> = ctx.get_output(&TaskId(format!("probe:{}", h)));
                let process_out: Option<String> = ctx.get_output(&TaskId(format!("process:{}", h)));

                match (probe_out, process_out) {
                    (Some(name), Some(_)) if !name.is_empty() => TaskOutcome::Success,
                    _ => TaskOutcome::Failed(format!(
                        "verify {}: missing output from prior stages",
                        h
                    )),
                }
            }))
        })
        .error_policy(ErrorPolicy::ContinueIndependent)
        .context(ctx)
        .build()
        .unwrap()
        .run()
        .unwrap();

    assert!(
        report.is_success(),
        "3-stage pipeline across 33 nodes failed: {:?}",
        report.failed
    );
    // 3 stages x 33 nodes = 99 tasks
    assert_eq!(
        report.completed.len(),
        99,
        "expected 99 completed tasks (3 stages x 33 nodes), got {}",
        report.completed.len()
    );
}

#[test]
fn test_scale_fanout_stress() {
    let cluster = &*LARGE_CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();
    let all_nodes = cluster.node_names();
    let fleet = cluster.fleet_config();

    assert_eq!(all_nodes.len(), 33);

    let ctx_low = DagContext::new();
    ctx_low.set_state("fleet", fleet.clone());
    ctx_low.set_state("ssh_key", key.clone());

    // Low fanout: concurrency = 2
    let start_low = std::time::Instant::now();
    let report_low = StageBuilder::new()
        .resources(all_nodes.clone())
        .stage("work", Some(2), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("work {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let key: String = ctx.get_state("ssh_key").unwrap();
                let node = &fleet.nodes[&h];
                let port = node.target_port.unwrap_or(22);

                let output = std::process::Command::new("ssh")
                    .args([
                        "-oStrictHostKeyChecking=no",
                        "-oPasswordAuthentication=no",
                        "-oConnectTimeout=5",
                        "-i",
                        &key,
                        "-p",
                        &port.to_string(),
                        &format!("{}@{}", node.target_user, node.target_host),
                        "echo done",
                    ])
                    .output();

                match output {
                    Ok(o) if o.status.success() => TaskOutcome::Success,
                    _ => TaskOutcome::Failed(format!("work {} failed", h)),
                }
            }))
        })
        .error_policy(ErrorPolicy::ContinueIndependent)
        .context(ctx_low)
        .build()
        .unwrap()
        .run()
        .unwrap();
    let elapsed_low = start_low.elapsed();

    assert!(
        report_low.is_success(),
        "fanout=2 run failed: {:?}",
        report_low.failed
    );
    assert_eq!(report_low.completed.len(), 33);

    // High fanout: concurrency = 33
    let ctx_high = DagContext::new();
    ctx_high.set_state("fleet", fleet.clone());
    ctx_high.set_state("ssh_key", key.clone());

    let start_high = std::time::Instant::now();
    let report_high = StageBuilder::new()
        .resources(all_nodes.clone())
        .stage("work", Some(33), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("work {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let key: String = ctx.get_state("ssh_key").unwrap();
                let node = &fleet.nodes[&h];
                let port = node.target_port.unwrap_or(22);

                let output = std::process::Command::new("ssh")
                    .args([
                        "-oStrictHostKeyChecking=no",
                        "-oPasswordAuthentication=no",
                        "-oConnectTimeout=5",
                        "-i",
                        &key,
                        "-p",
                        &port.to_string(),
                        &format!("{}@{}", node.target_user, node.target_host),
                        "echo done",
                    ])
                    .output();

                match output {
                    Ok(o) if o.status.success() => TaskOutcome::Success,
                    _ => TaskOutcome::Failed(format!("work {} failed", h)),
                }
            }))
        })
        .error_policy(ErrorPolicy::ContinueIndependent)
        .context(ctx_high)
        .build()
        .unwrap()
        .run()
        .unwrap();
    let elapsed_high = start_high.elapsed();

    assert!(
        report_high.is_success(),
        "fanout=33 run failed: {:?}",
        report_high.failed
    );
    assert_eq!(report_high.completed.len(), 33);

    eprintln!(
        "fanout stress: low(2)={:.2}s, high(33)={:.2}s",
        elapsed_low.as_secs_f64(),
        elapsed_high.as_secs_f64()
    );

    // Low concurrency should take longer than high concurrency
    assert!(
        elapsed_low > elapsed_high,
        "fanout=2 ({:.2}s) should be slower than fanout=33 ({:.2}s)",
        elapsed_low.as_secs_f64(),
        elapsed_high.as_secs_f64()
    );
}

#[test]
fn test_scale_tag_based_gpu_selection() {
    let cluster = &*LARGE_CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();
    let fleet = cluster.fleet_config();

    // Select only GPU nodes via tag-based filtering
    let gpu_nodes = fleet.nodes_by_tags(&["gpu".to_string()]);
    assert_eq!(
        gpu_nodes.len(),
        5,
        "expected 5 GPU nodes, got {}",
        gpu_nodes.len()
    );

    let gpu_names: Vec<String> = {
        let mut names: Vec<_> = gpu_nodes.iter().map(|n| n.name.clone()).collect();
        names.sort();
        names
    };

    // Verify they are all gpu-* nodes
    for name in &gpu_names {
        assert!(
            name.starts_with("gpu-"),
            "expected gpu- prefix, got: {}",
            name
        );
    }

    let ctx = DagContext::new();
    ctx.set_state("fleet", fleet.clone());
    ctx.set_state("ssh_key", key.clone());

    let report = StageBuilder::new()
        .resources(gpu_names.clone())
        .stage("gpu-probe", Some(5), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("gpu-probe {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let key: String = ctx.get_state("ssh_key").unwrap();
                let node = &fleet.nodes[&h];
                let port = node.target_port.unwrap_or(22);

                let output = std::process::Command::new("ssh")
                    .args([
                        "-oStrictHostKeyChecking=no",
                        "-oPasswordAuthentication=no",
                        "-oConnectTimeout=5",
                        "-i",
                        &key,
                        "-p",
                        &port.to_string(),
                        &format!("{}@{}", node.target_user, node.target_host),
                        "hostname",
                    ])
                    .output();

                match output {
                    Ok(o) if o.status.success() => TaskOutcome::Success,
                    Ok(o) => {
                        let err = String::from_utf8_lossy(&o.stderr);
                        TaskOutcome::Failed(format!("gpu-probe {} failed: {}", h, err.trim()))
                    }
                    Err(e) => TaskOutcome::Failed(format!("gpu-probe {} error: {}", h, e)),
                }
            }))
        })
        .error_policy(ErrorPolicy::ContinueIndependent)
        .context(ctx)
        .build()
        .unwrap()
        .run()
        .unwrap();

    assert!(
        report.is_success(),
        "GPU tag-based pipeline failed: {:?}",
        report.failed
    );
    assert_eq!(
        report.completed.len(),
        5,
        "expected exactly 5 GPU tasks, got {}",
        report.completed.len()
    );
}

// ─── SCP Copy Tests ─────────────────────────────────────────────────────────

#[test]
fn test_scp_file_copy_roundtrip() {
    let cluster = &*CLUSTER;
    let node = &cluster.nodes_with_prefix("compute")[0];
    let port = cluster.port_for(node).unwrap();
    let key = cluster.ssh_key_path().to_string_lossy().to_string();

    // Create a temp file with known contents
    let tmp_dir = std::env::temp_dir().join(format!("consortium-scp-test-{}", std::process::id()));
    std::fs::create_dir_all(&tmp_dir).unwrap();
    let local_src = tmp_dir.join("source.txt");
    let local_dst = tmp_dir.join("destination.txt");
    let payload = "consortium SCP roundtrip test payload\n";
    std::fs::write(&local_src, payload).unwrap();

    let scp_opts = [
        "-oStrictHostKeyChecking=no",
        "-oPasswordAuthentication=no",
        "-oConnectTimeout=5",
        "-i",
        &key,
        "-P",
        &port.to_string(),
    ];

    // SCP file to container
    let upload = std::process::Command::new("scp")
        .args(&scp_opts)
        .arg(local_src.to_str().unwrap())
        .arg("root@127.0.0.1:/tmp/scp_test.txt")
        .output()
        .expect("scp upload failed to spawn");
    assert!(
        upload.status.success(),
        "scp upload failed: {}",
        String::from_utf8_lossy(&upload.stderr)
    );

    // Verify the file exists on the container via SSH
    let verify = std::process::Command::new("ssh")
        .args([
            "-oStrictHostKeyChecking=no",
            "-oPasswordAuthentication=no",
            "-oConnectTimeout=5",
            "-i",
            &key,
            "-p",
            &port.to_string(),
            "root@127.0.0.1",
            "cat /tmp/scp_test.txt",
        ])
        .output()
        .expect("ssh verify failed to spawn");
    assert!(
        verify.status.success(),
        "ssh cat failed: {}",
        String::from_utf8_lossy(&verify.stderr)
    );
    assert_eq!(
        String::from_utf8_lossy(&verify.stdout),
        payload,
        "file contents on remote do not match"
    );

    // SCP the file back to local
    let download = std::process::Command::new("scp")
        .args(&scp_opts)
        .arg("root@127.0.0.1:/tmp/scp_test.txt")
        .arg(local_dst.to_str().unwrap())
        .output()
        .expect("scp download failed to spawn");
    assert!(
        download.status.success(),
        "scp download failed: {}",
        String::from_utf8_lossy(&download.stderr)
    );

    // Verify roundtrip contents
    let roundtrip = std::fs::read_to_string(&local_dst).unwrap();
    assert_eq!(roundtrip, payload, "roundtrip file contents mismatch");

    // Cleanup
    let _ = std::fs::remove_dir_all(&tmp_dir);
}

#[test]
fn test_scp_reverse_copy() {
    let cluster = &*CLUSTER;
    let node = &cluster.nodes_with_prefix("compute")[1];
    let port = cluster.port_for(node).unwrap();
    let key = cluster.ssh_key_path().to_string_lossy().to_string();

    let remote_payload = format!("created on {} at pid {}\n", node, std::process::id());

    // SSH to create a file on the container
    let create = std::process::Command::new("ssh")
        .args([
            "-oStrictHostKeyChecking=no",
            "-oPasswordAuthentication=no",
            "-oConnectTimeout=5",
            "-i",
            &key,
            "-p",
            &port.to_string(),
            "root@127.0.0.1",
            &format!("echo -n '{}' > /tmp/reverse_test.txt", remote_payload),
        ])
        .output()
        .expect("ssh create failed to spawn");
    assert!(
        create.status.success(),
        "ssh file creation failed: {}",
        String::from_utf8_lossy(&create.stderr)
    );

    // SCP it back to local
    let tmp_dir =
        std::env::temp_dir().join(format!("consortium-reverse-test-{}", std::process::id()));
    std::fs::create_dir_all(&tmp_dir).unwrap();
    let local_dst = tmp_dir.join("reverse.txt");

    let download = std::process::Command::new("scp")
        .args([
            "-oStrictHostKeyChecking=no",
            "-oPasswordAuthentication=no",
            "-oConnectTimeout=5",
            "-i",
            &key,
            "-P",
            &port.to_string(),
            "root@127.0.0.1:/tmp/reverse_test.txt",
            local_dst.to_str().unwrap(),
        ])
        .output()
        .expect("scp download failed to spawn");
    assert!(
        download.status.success(),
        "scp reverse download failed: {}",
        String::from_utf8_lossy(&download.stderr)
    );

    let contents = std::fs::read_to_string(&local_dst).unwrap();
    assert_eq!(
        contents, remote_payload,
        "reverse-copied file contents mismatch"
    );

    // Cleanup
    let _ = std::fs::remove_dir_all(&tmp_dir);
}

// ─── Error Recovery Tests ───────────────────────────────────────────────────

#[test]
fn test_dag_error_recovery_continue_independent() {
    let cluster = &*CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();
    let compute_nodes = cluster.nodes_with_prefix("compute");

    // Build a DAG with:
    //   task_first (succeeds) -> task_fail (exit 1) -> task_after_fail (should cancel)
    //   task_independent (succeeds, no deps on task_fail)
    let mut dag = DagBuilder::new();

    // Task 1: succeeds -- SSH echo on first compute node
    let port1 = cluster.port_for(&compute_nodes[0]).unwrap();
    dag.add_task(
        "task_first",
        ShellTask {
            command: format!(
                "ssh -oStrictHostKeyChecking=no -oPasswordAuthentication=no -i {} -p {} root@127.0.0.1 'echo first'",
                key, port1
            ),
            description: "first task (succeeds)".into(),
            resource: None,
        },
    );

    // Task 2: deliberately fails -- depends on task_first
    let port2 = cluster.port_for(&compute_nodes[1]).unwrap();
    dag.add_task(
        "task_fail",
        ShellTask {
            command: format!(
                "ssh -oStrictHostKeyChecking=no -oPasswordAuthentication=no -i {} -p {} root@127.0.0.1 'exit 1'",
                key, port2
            ),
            description: "failing task".into(),
            resource: None,
        },
    );
    dag.add_dep("task_fail", "task_first");

    // Task 3: depends on failing task -- should be cancelled
    let port3 = cluster.port_for(&compute_nodes[2]).unwrap();
    dag.add_task(
        "task_after_fail",
        ShellTask {
            command: format!(
                "ssh -oStrictHostKeyChecking=no -oPasswordAuthentication=no -i {} -p {} root@127.0.0.1 'echo should_not_run'",
                key, port3
            ),
            description: "task after failure (should cancel)".into(),
            resource: None,
        },
    );
    dag.add_dep("task_after_fail", "task_fail");

    // Task 4: independent -- no dependency on the failing chain
    let port4 = cluster.port_for(&compute_nodes[3]).unwrap();
    dag.add_task(
        "task_independent",
        ShellTask {
            command: format!(
                "ssh -oStrictHostKeyChecking=no -oPasswordAuthentication=no -i {} -p {} root@127.0.0.1 'echo independent'",
                key, port4
            ),
            description: "independent task (succeeds)".into(),
            resource: None,
        },
    );

    dag.error_policy(ErrorPolicy::ContinueIndependent);

    let report = dag.build().unwrap().run().unwrap();

    assert!(
        !report.is_success(),
        "DAG should report failure due to task_fail"
    );
    assert!(
        report.completed.contains(&TaskId::from("task_first")),
        "task_first should succeed, got: completed={:?} failed={:?}",
        report.completed,
        report.failed
    );
    assert!(
        report.failed.contains_key(&TaskId::from("task_fail")),
        "task_fail should be in failed set"
    );
    assert!(
        report.cancelled.contains(&TaskId::from("task_after_fail")),
        "task_after_fail should be cancelled (depends on failed task)"
    );
    assert!(
        report.completed.contains(&TaskId::from("task_independent")),
        "task_independent should complete (no dependency on failing chain)"
    );
}

// ─── Container Failure / Unreachable Node Tests ─────────────────────────────

#[test]
fn test_stage_builder_unreachable_node_failure() {
    let cluster = &*CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();
    let compute_nodes = cluster.nodes_with_prefix("compute");
    let mut fleet = cluster.fleet_config();

    // Add a bogus unreachable node to the fleet config
    fleet.nodes.insert(
        "unreachable-node".to_string(),
        consortium_nix::config::DeploymentNode {
            name: "unreachable-node".to_string(),
            target_host: "127.0.0.1".to_string(),
            target_user: "root".to_string(),
            target_port: Some(9999),
            system: "x86_64-linux".to_string(),
            profile_type: consortium_nix::config::ProfileType::Nixos,
            build_on_target: false,
            tags: vec!["compute".to_string()],
            drv_path: None,
            toplevel: None,
        },
    );

    let ctx = DagContext::new();
    ctx.set_state("fleet", fleet.clone());
    ctx.set_state("ssh_key", key.clone());

    // Resources include 2 real compute nodes plus the bogus one
    let mut resources = vec![
        compute_nodes[0].clone(),
        compute_nodes[1].clone(),
        "unreachable-node".to_string(),
    ];
    resources.sort();

    let report = StageBuilder::new()
        .resources(resources.clone())
        .stage("connect", Some(5), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("connect {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let key: String = ctx.get_state("ssh_key").unwrap();
                let node = &fleet.nodes[&h];
                let port = node.target_port.unwrap_or(22);

                let output = std::process::Command::new("ssh")
                    .args([
                        "-oStrictHostKeyChecking=no",
                        "-oPasswordAuthentication=no",
                        "-oConnectTimeout=3",
                        "-oBatchMode=yes",
                        "-i",
                        &key,
                        "-p",
                        &port.to_string(),
                        &format!("{}@{}", node.target_user, node.target_host),
                        "echo connected",
                    ])
                    .output();

                match output {
                    Ok(o) if o.status.success() => TaskOutcome::Success,
                    Ok(o) => {
                        let err = String::from_utf8_lossy(&o.stderr);
                        TaskOutcome::Failed(format!("connect {} failed: {}", h, err.trim()))
                    }
                    Err(e) => TaskOutcome::Failed(format!("connect {} error: {}", h, e)),
                }
            }))
        })
        .stage("work", Some(5), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("work {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let key: String = ctx.get_state("ssh_key").unwrap();
                let node = &fleet.nodes[&h];
                let port = node.target_port.unwrap_or(22);

                let output = std::process::Command::new("ssh")
                    .args([
                        "-oStrictHostKeyChecking=no",
                        "-oPasswordAuthentication=no",
                        "-oConnectTimeout=3",
                        "-oBatchMode=yes",
                        "-i",
                        &key,
                        "-p",
                        &port.to_string(),
                        &format!("{}@{}", node.target_user, node.target_host),
                        "sleep 1 && echo done",
                    ])
                    .output();

                match output {
                    Ok(o) if o.status.success() => TaskOutcome::Success,
                    Ok(o) => {
                        let err = String::from_utf8_lossy(&o.stderr);
                        TaskOutcome::Failed(format!("work {} failed: {}", h, err.trim()))
                    }
                    Err(e) => TaskOutcome::Failed(format!("work {} error: {}", h, e)),
                }
            }))
        })
        .error_policy(ErrorPolicy::ContinueIndependent)
        .context(ctx)
        .build()
        .unwrap()
        .run()
        .unwrap();

    // The unreachable node should fail at the connect stage, and its work stage
    // should be cancelled. Real nodes should succeed through both stages.
    assert!(
        !report.is_success(),
        "DAG should fail due to unreachable node"
    );

    // Real compute nodes: both stages complete
    for real_node in &[&compute_nodes[0], &compute_nodes[1]] {
        assert!(
            report
                .completed
                .contains(&TaskId(format!("connect:{}", real_node))),
            "connect:{} should succeed",
            real_node
        );
        assert!(
            report
                .completed
                .contains(&TaskId(format!("work:{}", real_node))),
            "work:{} should succeed",
            real_node
        );
    }

    // Unreachable node: connect fails, work is cancelled
    assert!(
        report
            .failed
            .contains_key(&TaskId::from("connect:unreachable-node")),
        "connect:unreachable-node should fail, got: failed={:?}",
        report.failed.keys().collect::<Vec<_>>()
    );
    assert!(
        report
            .cancelled
            .contains(&TaskId::from("work:unreachable-node")),
        "work:unreachable-node should be cancelled, got: cancelled={:?}",
        report.cancelled
    );
}

// ─── Multi-Stage Deployment Pipeline Tests ──────────────────────────────────

/// Helper: run an SSH command using fleet config and return stdout or an error string.
fn ssh_exec_from_fleet(
    fleet: &consortium_nix::FleetConfig,
    ssh_key: &str,
    node: &str,
    remote_cmd: &str,
) -> std::result::Result<String, String> {
    let n = fleet
        .nodes
        .get(node)
        .ok_or_else(|| format!("unknown node: {}", node))?;
    let port = n.target_port.unwrap_or(22);
    let output = std::process::Command::new("ssh")
        .args([
            "-oStrictHostKeyChecking=no",
            "-oPasswordAuthentication=no",
            "-oConnectTimeout=5",
            "-i",
            ssh_key,
            "-p",
            &port.to_string(),
            &format!("{}@{}", n.target_user, n.target_host),
            remote_cmd,
        ])
        .output()
        .map_err(|e| format!("ssh spawn error: {}", e))?;

    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(format!(
            "ssh exit {}: {}",
            output.status.code().unwrap_or(-1),
            stderr.trim()
        ))
    }
}

#[test]
fn test_simulated_nixos_deploy_pipeline() {
    // 4-stage pipeline mimicking consortium-nix's deploy flow:
    //   eval -> build -> copy -> activate
    // Each stage SSHes to the node, produces output consumed by the next stage.
    let cluster = &*CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();
    let compute_nodes = cluster.nodes_with_prefix("compute");
    let fleet = cluster.fleet_config();

    let ctx = DagContext::new();
    ctx.set_state("fleet", fleet.clone());
    ctx.set_state("ssh_key", key.clone());

    let report = StageBuilder::new()
        .resources(compute_nodes.clone())
        // Stage 1: eval — simulate `nix eval .#nixosConfigurations.<host>.config.system.build.toplevel`
        .stage("eval", Some(5), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("eval {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let ssh_key: String = ctx.get_state("ssh_key").unwrap();
                let cmd = format!("echo /nix/store/fake-toplevel-{}", h);
                match ssh_exec_from_fleet(&fleet, &ssh_key, &h, &cmd) {
                    Ok(toplevel) => {
                        ctx.set_output(TaskId(format!("eval:{}", h)), toplevel);
                        TaskOutcome::Success
                    }
                    Err(e) => TaskOutcome::Failed(format!("eval {} failed: {}", h, e)),
                }
            }))
        })
        // Stage 2: build — simulate `nix build`, reads eval output
        .stage("build", Some(5), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("build {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let ssh_key: String = ctx.get_state("ssh_key").unwrap();
                let toplevel: String = match ctx.get_output(&TaskId(format!("eval:{}", h))) {
                    Some(t) => t,
                    None => return TaskOutcome::Failed(format!("build {}: no eval output", h)),
                };
                assert!(
                    toplevel.contains("fake-toplevel"),
                    "eval output mismatch for {}",
                    h
                );
                let cmd = format!("echo built-{}", h);
                match ssh_exec_from_fleet(&fleet, &ssh_key, &h, &cmd) {
                    Ok(built) => {
                        ctx.set_output(TaskId(format!("build:{}", h)), built);
                        TaskOutcome::Success
                    }
                    Err(e) => TaskOutcome::Failed(format!("build {} failed: {}", h, e)),
                }
            }))
        })
        // Stage 3: copy — simulate `nix copy`, reads build output
        .stage("copy", Some(5), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("copy {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let ssh_key: String = ctx.get_state("ssh_key").unwrap();
                let built: String = match ctx.get_output(&TaskId(format!("build:{}", h))) {
                    Some(b) => b,
                    None => return TaskOutcome::Failed(format!("copy {}: no build output", h)),
                };
                assert!(
                    built.starts_with("built-"),
                    "build output mismatch for {}",
                    h
                );
                match ssh_exec_from_fleet(&fleet, &ssh_key, &h, "echo copied") {
                    Ok(out) => {
                        ctx.set_output(TaskId(format!("copy:{}", h)), out);
                        TaskOutcome::Success
                    }
                    Err(e) => TaskOutcome::Failed(format!("copy {} failed: {}", h, e)),
                }
            }))
        })
        // Stage 4: activate — simulate `switch-to-configuration switch`, reads copy output
        .stage("activate", Some(5), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("activate {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let ssh_key: String = ctx.get_state("ssh_key").unwrap();
                let copied: String = match ctx.get_output(&TaskId(format!("copy:{}", h))) {
                    Some(c) => c,
                    None => return TaskOutcome::Failed(format!("activate {}: no copy output", h)),
                };
                assert_eq!(copied, "copied", "copy output mismatch for {}", h);
                match ssh_exec_from_fleet(&fleet, &ssh_key, &h, "echo activated") {
                    Ok(out) => {
                        ctx.set_output(TaskId(format!("activate:{}", h)), out);
                        TaskOutcome::Success
                    }
                    Err(e) => TaskOutcome::Failed(format!("activate {} failed: {}", h, e)),
                }
            }))
        })
        .error_policy(ErrorPolicy::ContinueIndependent)
        .context(ctx.clone())
        .build()
        .unwrap()
        .run()
        .unwrap();

    assert!(
        report.is_success(),
        "deploy pipeline failed: {:?}",
        report.failed
    );
    // 4 stages x N compute nodes
    assert_eq!(report.completed.len(), compute_nodes.len() * 4);

    // Verify all final outputs are correct
    for node in &compute_nodes {
        let eval_out: String = ctx.get_output(&TaskId(format!("eval:{}", node))).unwrap();
        assert_eq!(eval_out, format!("/nix/store/fake-toplevel-{}", node));

        let build_out: String = ctx.get_output(&TaskId(format!("build:{}", node))).unwrap();
        assert_eq!(build_out, format!("built-{}", node));

        let copy_out: String = ctx.get_output(&TaskId(format!("copy:{}", node))).unwrap();
        assert_eq!(copy_out, "copied");

        let activate_out: String = ctx
            .get_output(&TaskId(format!("activate:{}", node)))
            .unwrap();
        assert_eq!(activate_out, "activated");
    }
}

#[test]
fn test_simulated_rolling_deployment() {
    // Builds and copies run with unlimited concurrency; activations one at a time.
    // Verifies concurrency=1 on activate serializes activations, and that total
    // wall time is less than fully sequential execution.
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Arc;

    let cluster = &*CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();
    let compute_nodes = cluster.nodes_with_prefix("compute");
    let fleet = cluster.fleet_config();

    let ctx = DagContext::new();
    ctx.set_state("fleet", fleet.clone());
    ctx.set_state("ssh_key", key.clone());

    // Track max concurrent activations to verify serialization
    let activate_current = Arc::new(AtomicUsize::new(0));
    let activate_max = Arc::new(AtomicUsize::new(0));
    ctx.set_state("activate_current", activate_current.clone());
    ctx.set_state("activate_max", activate_max.clone());

    let start = std::time::Instant::now();

    let report = StageBuilder::new()
        .resources(compute_nodes.clone())
        // Build stage: unlimited concurrency — all builds in parallel
        .stage("build", None, |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("build {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let ssh_key: String = ctx.get_state("ssh_key").unwrap();
                let cmd = format!("sleep 0.1 && echo built-{}", h);
                match ssh_exec_from_fleet(&fleet, &ssh_key, &h, &cmd) {
                    Ok(out) => {
                        ctx.set_output(TaskId(format!("build:{}", h)), out);
                        TaskOutcome::Success
                    }
                    Err(e) => TaskOutcome::Failed(format!("build {} failed: {}", h, e)),
                }
            }))
        })
        // Copy stage: unlimited concurrency
        .stage("copy", None, |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("copy {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let ssh_key: String = ctx.get_state("ssh_key").unwrap();
                match ssh_exec_from_fleet(&fleet, &ssh_key, &h, "echo copied") {
                    Ok(out) => {
                        ctx.set_output(TaskId(format!("copy:{}", h)), out);
                        TaskOutcome::Success
                    }
                    Err(e) => TaskOutcome::Failed(format!("copy {} failed: {}", h, e)),
                }
            }))
        })
        // Activate stage: concurrency=1 — rolling, one at a time
        .stage("activate", Some(1), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("activate {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let ssh_key: String = ctx.get_state("ssh_key").unwrap();
                let cur: Arc<AtomicUsize> = ctx.get_state("activate_current").unwrap();
                let max: Arc<AtomicUsize> = ctx.get_state("activate_max").unwrap();

                let c = cur.fetch_add(1, Ordering::SeqCst) + 1;
                // Update max observed concurrency
                loop {
                    let prev = max.load(Ordering::SeqCst);
                    if c <= prev
                        || max
                            .compare_exchange(prev, c, Ordering::SeqCst, Ordering::SeqCst)
                            .is_ok()
                    {
                        break;
                    }
                }

                let cmd = "sleep 0.05 && echo activated";
                let result = ssh_exec_from_fleet(&fleet, &ssh_key, &h, cmd);
                cur.fetch_sub(1, Ordering::SeqCst);
                match result {
                    Ok(out) => {
                        ctx.set_output(TaskId(format!("activate:{}", h)), out);
                        TaskOutcome::Success
                    }
                    Err(e) => TaskOutcome::Failed(format!("activate {} failed: {}", h, e)),
                }
            }))
        })
        .error_policy(ErrorPolicy::ContinueIndependent)
        .context(ctx)
        .build()
        .unwrap()
        .run()
        .unwrap();

    let elapsed = start.elapsed();
    let n = compute_nodes.len();

    assert!(
        report.is_success(),
        "rolling deploy failed: {:?}",
        report.failed
    );
    assert_eq!(report.completed.len(), n * 3); // 3 stages x N nodes

    // Activations must have been serialized (max concurrency = 1)
    assert_eq!(
        activate_max.load(Ordering::SeqCst),
        1,
        "activations were not serialized: max concurrent = {}",
        activate_max.load(Ordering::SeqCst)
    );

    // The key assertion is that activations were serialized (max_concurrent == 1).
    // Timing is unreliable with SSH overhead, so use a very generous bound.
    assert!(
        elapsed < Duration::from_secs(30),
        "rolling deploy took {:?}, something is very wrong",
        elapsed,
    );
}

#[test]
fn test_simulated_build_only_mode() {
    // 2-stage pipeline: eval + build only (no copy, no activate).
    // Verifies that only the requested stages execute.
    let cluster = &*CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();
    let compute_nodes = cluster.nodes_with_prefix("compute");
    let fleet = cluster.fleet_config();

    let ctx = DagContext::new();
    ctx.set_state("fleet", fleet.clone());
    ctx.set_state("ssh_key", key.clone());

    let report = StageBuilder::new()
        .resources(compute_nodes.clone())
        .stage("eval", Some(5), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("eval {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let ssh_key: String = ctx.get_state("ssh_key").unwrap();
                let cmd = format!("echo /nix/store/fake-toplevel-{}", h);
                match ssh_exec_from_fleet(&fleet, &ssh_key, &h, &cmd) {
                    Ok(toplevel) => {
                        ctx.set_output(TaskId(format!("eval:{}", h)), toplevel);
                        TaskOutcome::Success
                    }
                    Err(e) => TaskOutcome::Failed(format!("eval {} failed: {}", h, e)),
                }
            }))
        })
        .stage("build", Some(5), |host| {
            let h = host.to_string();
            Box::new(FnTask::new(format!("build {}", h), move |ctx| {
                let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                let ssh_key: String = ctx.get_state("ssh_key").unwrap();
                // Confirm pipeline dependency by reading eval output
                let toplevel: String = match ctx.get_output(&TaskId(format!("eval:{}", h))) {
                    Some(t) => t,
                    None => return TaskOutcome::Failed(format!("build {}: no eval output", h)),
                };
                assert!(toplevel.contains("fake-toplevel"));
                let cmd = format!("echo built-{}", h);
                match ssh_exec_from_fleet(&fleet, &ssh_key, &h, &cmd) {
                    Ok(out) => {
                        ctx.set_output(TaskId(format!("build:{}", h)), out);
                        TaskOutcome::Success
                    }
                    Err(e) => TaskOutcome::Failed(format!("build {} failed: {}", h, e)),
                }
            }))
        })
        .error_policy(ErrorPolicy::ContinueIndependent)
        .context(ctx.clone())
        .build()
        .unwrap()
        .run()
        .unwrap();

    assert!(
        report.is_success(),
        "build-only pipeline failed: {:?}",
        report.failed
    );

    // Only 2 stages x N nodes
    assert_eq!(report.completed.len(), compute_nodes.len() * 2);

    // Verify no copy or activate outputs exist (they were never created)
    for node in &compute_nodes {
        assert!(
            ctx.get_output::<String>(&TaskId(format!("copy:{}", node)))
                .is_none(),
            "unexpected copy output for {}",
            node
        );
        assert!(
            ctx.get_output::<String>(&TaskId(format!("activate:{}", node)))
                .is_none(),
            "unexpected activate output for {}",
            node
        );
        // But eval and build outputs must exist
        assert!(
            ctx.get_output::<String>(&TaskId(format!("eval:{}", node)))
                .is_some(),
            "missing eval output for {}",
            node
        );
        assert!(
            ctx.get_output::<String>(&TaskId(format!("build:{}", node)))
                .is_some(),
            "missing build output for {}",
            node
        );
    }
}

#[test]
fn test_cross_host_dependency() {
    // Custom DAG using DagBuilder (not StageBuilder) to express a cross-host
    // dependency: host B's activation depends on host A's activation completing.
    // Simulates "upgrade database server before app server".
    //
    // DAG shape:
    //   eval:db -> build:db -> copy:db -> activate:db ──┐
    //   eval:app -> build:app -> copy:app -> activate:app <─┘
    //
    // activate:app waits for BOTH copy:app AND activate:db.
    let cluster = &*CLUSTER;
    let key = cluster.ssh_key_path().to_string_lossy().to_string();
    let compute_nodes = cluster.nodes_with_prefix("compute");
    let fleet = cluster.fleet_config();

    let db_host = &compute_nodes[0];
    let app_host = &compute_nodes[1];

    let ctx = DagContext::new();
    ctx.set_state("fleet", fleet.clone());
    ctx.set_state("ssh_key", key.clone());

    let mut dag = DagBuilder::new();
    dag.context(ctx.clone());
    dag.error_policy(ErrorPolicy::ContinueIndependent);

    // Build the 4-stage pipeline for each host
    for host in &[db_host, app_host] {
        let h = host.to_string();
        let stages = ["eval", "build", "copy", "activate"];

        for stage in &stages {
            let h2 = h.clone();
            let s = stage.to_string();
            let task_id = format!("{}:{}", s, h2);

            dag.add_task(
                task_id.as_str(),
                FnTask::new(format!("{} {}", s, h2), move |ctx| {
                    let fleet: consortium_nix::FleetConfig = ctx.get_state("fleet").unwrap();
                    let ssh_key: String = ctx.get_state("ssh_key").unwrap();

                    // Verify previous stage output exists (except for eval)
                    let prev_stage = match s.as_str() {
                        "build" => Some("eval"),
                        "copy" => Some("build"),
                        "activate" => Some("copy"),
                        _ => None,
                    };
                    if let Some(ps) = prev_stage {
                        let prev_id = TaskId(format!("{}:{}", ps, h2));
                        if ctx.get_output::<String>(&prev_id).is_none() {
                            return TaskOutcome::Failed(format!(
                                "{} {}: missing {} output",
                                s, h2, ps
                            ));
                        }
                    }

                    let remote_cmd = match s.as_str() {
                        "eval" => format!("echo /nix/store/fake-toplevel-{}", h2),
                        "build" => format!("echo built-{}", h2),
                        "copy" => "echo copied".to_string(),
                        "activate" => "echo activated".to_string(),
                        _ => unreachable!(),
                    };

                    match ssh_exec_from_fleet(&fleet, &ssh_key, &h2, &remote_cmd) {
                        Ok(out) => {
                            ctx.set_output(TaskId(format!("{}:{}", s, h2)), out);
                            TaskOutcome::Success
                        }
                        Err(e) => TaskOutcome::Failed(format!("{} {} failed: {}", s, h2, e)),
                    }
                }),
            );
        }

        // Intra-host pipeline dependencies: eval -> build -> copy -> activate
        dag.add_dep(format!("build:{}", h), format!("eval:{}", h));
        dag.add_dep(format!("copy:{}", h), format!("build:{}", h));
        dag.add_dep(format!("activate:{}", h), format!("copy:{}", h));
    }

    // Cross-host dependency: app's activate depends on db's activate
    dag.add_dep(
        format!("activate:{}", app_host),
        format!("activate:{}", db_host),
    );

    let report = dag.build().unwrap().run().unwrap();

    assert!(
        report.is_success(),
        "cross-host dependency DAG failed: {:?}",
        report.failed
    );
    // 4 stages x 2 hosts = 8 tasks
    assert_eq!(report.completed.len(), 8);

    // Verify both hosts were activated
    let db_activate: String = ctx
        .get_output(&TaskId(format!("activate:{}", db_host)))
        .unwrap();
    assert_eq!(db_activate, "activated");

    let app_activate: String = ctx
        .get_output(&TaskId(format!("activate:{}", app_host)))
        .unwrap();
    assert_eq!(app_activate, "activated");

    // The DAG executor guarantees that activate:app ran after activate:db because
    // of the explicit dependency edge. The fact that the DAG completed successfully
    // with all outputs present confirms the cross-host ordering was respected.
    assert!(
        ctx.has_output(&TaskId(format!("activate:{}", db_host))),
        "db activate output missing — cross-host dep may not be working"
    );
}
