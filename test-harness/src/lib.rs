//! Docker-based mini-HPC test harness for consortium.
//!
//! Provides a `DockerCluster` that manages a fleet of Alpine SSH containers
//! for integration testing. The cluster is started once per test binary via
//! `LazyLock` and torn down on exit.
//!
//! # Usage
//!
//! ```rust,ignore
//! use consortium_test_harness::DockerCluster;
//! use std::sync::LazyLock;
//!
//! static CLUSTER: LazyLock<DockerCluster> = LazyLock::new(|| {
//!     DockerCluster::start_default().expect("docker cluster failed")
//! });
//!
//! #[test]
//! fn test_ssh_to_node() {
//!     let cluster = &*CLUSTER;
//!     let opts = cluster.ssh_options();
//!     // ... use SshWorker with opts
//! }
//! ```

use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant};

use consortium::worker::ssh::SshOptions;
use consortium_nix::config::{DeploymentNode, FleetConfig, ProfileType};

/// Cluster topology configuration.
#[derive(Debug, Clone)]
pub struct ClusterTopology {
    pub compute_count: usize,
    pub gpu_count: usize,
    pub login_count: usize,
    pub controller: bool,
}

impl Default for ClusterTopology {
    fn default() -> Self {
        Self {
            compute_count: 25,
            gpu_count: 5,
            login_count: 2,
            controller: true,
        }
    }
}

/// A running Docker Compose cluster for integration testing.
pub struct DockerCluster {
    compose_file: PathBuf,
    project_name: String,
    docker_dir: PathBuf,
    ssh_key_path: PathBuf,
    /// Map of node name → host port (for SSH access from host).
    node_ports: HashMap<String, u16>,
    topology: ClusterTopology,
}

/// Base port for SSH port mapping. compute-01 gets 2201, etc.
const BASE_PORT: u16 = 2200;

impl DockerCluster {
    /// Start a cluster with the default topology (25 compute + 5 GPU + 2 login + 1 controller).
    pub fn start_default() -> Result<Self, String> {
        Self::start(ClusterTopology::default())
    }

    /// Start a cluster with a small topology for quick tests.
    pub fn start_small() -> Result<Self, String> {
        Self::start(ClusterTopology {
            compute_count: 5,
            gpu_count: 0,
            login_count: 1,
            controller: false,
        })
    }

    /// Start a cluster with the given topology.
    pub fn start(topology: ClusterTopology) -> Result<Self, String> {
        // Clean up any stale containers from previous runs
        Self::cleanup_stale();

        // Check Docker is available
        let docker_check = Command::new("docker")
            .arg("info")
            .output()
            .map_err(|e| format!("docker not found: {}", e))?;
        if !docker_check.status.success() {
            return Err("docker daemon not running".into());
        }

        let docker_dir = Self::docker_dir();
        let project_name = format!("consortium-test-{}", std::process::id());

        // Generate SSH keys
        let ssh_key_path = Self::generate_ssh_keys(&docker_dir)?;

        // Generate compose file
        let (compose_file, node_ports) =
            Self::generate_compose(&docker_dir, &topology, &project_name)?;

        let cluster = Self {
            compose_file,
            project_name,
            docker_dir,
            ssh_key_path,
            node_ports,
            topology,
        };

        // Start containers
        cluster.compose_up()?;

        // Wait for all nodes to be ready
        cluster.wait_ready(Duration::from_secs(120))?;

        Ok(cluster)
    }

    /// Stop the cluster and remove containers.
    pub fn stop(&self) -> Result<(), String> {
        let output = Command::new("docker")
            .args([
                "compose",
                "-f",
                self.compose_file.to_str().unwrap(),
                "-p",
                &self.project_name,
                "down",
                "-v",
                "--remove-orphans",
            ])
            .output()
            .map_err(|e| format!("docker compose down failed: {}", e))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(format!("compose down failed: {}", stderr));
        }

        Ok(())
    }

    /// Get the SSH identity file path.
    pub fn ssh_key_path(&self) -> &Path {
        &self.ssh_key_path
    }

    /// Get SshOptions configured for this cluster.
    pub fn ssh_options(&self) -> SshOptions {
        SshOptions {
            identity_file: Some(self.ssh_key_path.to_string_lossy().to_string()),
            strict_host_key_checking: false,
            password_auth: false,
            connect_timeout: Some(5),
            ..SshOptions::default()
        }
    }

    /// Get SshOptions for a specific node (includes port).
    pub fn ssh_options_for(&self, node: &str) -> Option<SshOptions> {
        let port = self.node_ports.get(node)?;
        Some(SshOptions {
            identity_file: Some(self.ssh_key_path.to_string_lossy().to_string()),
            port: Some(*port),
            strict_host_key_checking: false,
            password_auth: false,
            connect_timeout: Some(5),
            ..SshOptions::default()
        })
    }

    /// Get the host port for a node.
    pub fn port_for(&self, node: &str) -> Option<u16> {
        self.node_ports.get(node).copied()
    }

    /// Get all node names.
    pub fn node_names(&self) -> Vec<String> {
        let mut names: Vec<_> = self.node_ports.keys().cloned().collect();
        names.sort();
        names
    }

    /// Get node names matching a prefix (e.g. "compute" returns compute-01..compute-25).
    pub fn nodes_with_prefix(&self, prefix: &str) -> Vec<String> {
        let mut names: Vec<_> = self
            .node_ports
            .keys()
            .filter(|n| n.starts_with(prefix))
            .cloned()
            .collect();
        names.sort();
        names
    }

    /// Generate a FleetConfig for this cluster.
    pub fn fleet_config(&self) -> FleetConfig {
        let mut nodes = HashMap::new();
        for (name, port) in &self.node_ports {
            let tags = Self::infer_tags(name);
            nodes.insert(
                name.clone(),
                DeploymentNode {
                    name: name.clone(),
                    target_host: "127.0.0.1".to_string(),
                    target_user: "root".to_string(),
                    target_port: Some(*port),
                    system: "x86_64-linux".to_string(),
                    profile_type: ProfileType::Nixos,
                    build_on_target: false,
                    tags,
                    drv_path: None,
                    toplevel: None,
                },
            );
        }
        FleetConfig {
            nodes,
            builders: HashMap::new(),
            flake_uri: ".".to_string(),
            ansible_config: None,
            slurm_config: None,
            ray_config: None,
            skypilot_config: None,
        }
    }

    /// Total number of nodes.
    pub fn node_count(&self) -> usize {
        self.node_ports.len()
    }

    // ─── Internal ────────────────────────────────────────────────────────

    /// Clean up any stale consortium test containers from previous runs.
    fn cleanup_stale() {
        // Remove containers
        let _ = Command::new("sh")
            .args([
                "-c",
                "docker ps -a --filter 'name=consortium-test' -q | xargs -r docker rm -f 2>/dev/null",
            ])
            .output();
        // Remove networks
        let _ = Command::new("sh")
            .args([
                "-c",
                "docker network ls --filter 'name=consortium-test' -q | xargs -r docker network rm 2>/dev/null",
            ])
            .output();
    }

    fn docker_dir() -> PathBuf {
        let manifest = std::env::var("CARGO_MANIFEST_DIR").unwrap_or_else(|_| ".".to_string());
        PathBuf::from(manifest)
            .parent()
            .unwrap()
            .parent()
            .unwrap()
            .join("tests")
            .join("docker")
    }

    fn generate_ssh_keys(docker_dir: &Path) -> Result<PathBuf, String> {
        let ssh_dir = docker_dir.join("ssh");
        std::fs::create_dir_all(&ssh_dir)
            .map_err(|e| format!("failed to create ssh dir: {}", e))?;

        let key_path = ssh_dir.join("id_ed25519");
        if !key_path.exists() {
            let output = Command::new("ssh-keygen")
                .args([
                    "-t",
                    "ed25519",
                    "-f",
                    key_path.to_str().unwrap(),
                    "-N",
                    "",
                    "-q",
                ])
                .output()
                .map_err(|e| format!("ssh-keygen failed: {}", e))?;

            if !output.status.success() {
                return Err("ssh-keygen failed".into());
            }
        }

        // Copy pub key to authorized_keys
        let pub_key = std::fs::read_to_string(ssh_dir.join("id_ed25519.pub"))
            .map_err(|e| format!("read pub key: {}", e))?;
        std::fs::write(ssh_dir.join("authorized_keys"), &pub_key)
            .map_err(|e| format!("write authorized_keys: {}", e))?;

        Ok(key_path)
    }

    fn generate_compose(
        docker_dir: &Path,
        topology: &ClusterTopology,
        _project_name: &str,
    ) -> Result<(PathBuf, HashMap<String, u16>), String> {
        let mut services = Vec::new();
        let mut ports = HashMap::new();
        let mut port = BASE_PORT + 1;

        let anchor = r#"x-ssh-node: &ssh-node
  build:
    context: .
    dockerfile: Dockerfile.ssh-node
  volumes:
    - ./ssh/authorized_keys:/root/.ssh/authorized_keys:ro
  networks:
    - cluster
  restart: "no""#;

        // Compute nodes
        for i in 1..=topology.compute_count {
            let name = format!("compute-{:02}", i);
            services.push(format!(
                "  {}:\n    <<: *ssh-node\n    hostname: {}\n    ports:\n      - \"{}:22\"",
                name, name, port
            ));
            ports.insert(name, port);
            port += 1;
        }

        // GPU nodes
        for i in 1..=topology.gpu_count {
            let name = format!("gpu-{:02}", i);
            services.push(format!(
                "  {}:\n    <<: *ssh-node\n    hostname: {}\n    ports:\n      - \"{}:22\"",
                name, name, port
            ));
            ports.insert(name, port);
            port += 1;
        }

        // Login nodes
        for i in 1..=topology.login_count {
            let name = format!("login-{:02}", i);
            services.push(format!(
                "  {}:\n    <<: *ssh-node\n    hostname: {}\n    ports:\n      - \"{}:22\"",
                name, name, port
            ));
            ports.insert(name, port);
            port += 1;
        }

        // Controller
        if topology.controller {
            let name = "controller".to_string();
            services.push(format!(
                "  {}:\n    <<: *ssh-node\n    hostname: {}\n    ports:\n      - \"{}:22\"",
                name, name, port
            ));
            ports.insert(name, port);
        }

        let yaml = format!(
            "{}\n\nservices:\n{}\n\nnetworks:\n  cluster:\n    driver: bridge\n",
            anchor,
            services.join("\n\n")
        );

        let compose_path = docker_dir.join("docker-compose.generated.yml");
        let mut f = std::fs::File::create(&compose_path)
            .map_err(|e| format!("create compose file: {}", e))?;
        f.write_all(yaml.as_bytes())
            .map_err(|e| format!("write compose file: {}", e))?;

        Ok((compose_path, ports))
    }

    fn compose_up(&self) -> Result<(), String> {
        let output = Command::new("docker")
            .args([
                "compose",
                "-f",
                self.compose_file.to_str().unwrap(),
                "-p",
                &self.project_name,
                "up",
                "-d",
                "--build",
                "--wait",
            ])
            .output()
            .map_err(|e| format!("docker compose up failed: {}", e))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            return Err(format!("compose up failed: {}", stderr));
        }

        Ok(())
    }

    fn wait_ready(&self, timeout: Duration) -> Result<(), String> {
        let start = Instant::now();

        for (name, port) in &self.node_ports {
            loop {
                if start.elapsed() > timeout {
                    return Err(format!("timeout waiting for {} (port {})", name, port));
                }

                // Try SSH connection
                let result = Command::new("ssh")
                    .args([
                        "-oStrictHostKeyChecking=no",
                        "-oPasswordAuthentication=no",
                        "-oConnectTimeout=2",
                        "-oBatchMode=yes",
                        "-i",
                        self.ssh_key_path.to_str().unwrap(),
                        "-p",
                        &port.to_string(),
                        "root@127.0.0.1",
                        "true",
                    ])
                    .output();

                if let Ok(o) = result {
                    if o.status.success() {
                        break;
                    }
                }

                std::thread::sleep(Duration::from_millis(500));
            }
        }

        Ok(())
    }

    fn infer_tags(name: &str) -> Vec<String> {
        let mut tags = Vec::new();
        if name.starts_with("compute") {
            tags.push("compute".to_string());
        }
        if name.starts_with("gpu") {
            tags.push("gpu".to_string());
            tags.push("compute".to_string());
        }
        if name.starts_with("login") {
            tags.push("login".to_string());
        }
        if name == "controller" {
            tags.push("controller".to_string());
        }
        tags
    }
}

impl Drop for DockerCluster {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

/// Check if Docker is available. Returns false if docker is not installed
/// or the daemon is not running.
pub fn docker_available() -> bool {
    Command::new("docker")
        .arg("info")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}
