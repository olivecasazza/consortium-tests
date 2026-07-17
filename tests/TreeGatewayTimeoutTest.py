"""
Unit test for gateway channel timeout handling (fix for FIXME in Task._pchannel).

Verifies that:
  1. Gateway channel workers receive connect_timeout from task info.
  2. The timeout is invalidated once the channel is established (StartMessage).
"""

import logging
import unittest
from textwrap import dedent
from unittest.mock import patch, MagicMock

from ClusterShell.Task import task_self
from ClusterShell.Propagation import PropagationChannel, RouteResolvingError
from ClusterShell.NodeSet import NodeSet

from .TLib import HOSTNAME, make_temp_file

logging.basicConfig(level=logging.DEBUG)


class TreeGatewayTimeoutTest(unittest.TestCase):
    """Test cases for gateway channel timeout handling"""

    def tearDown(self):
        """clear task topology and gateways"""
        task = task_self()
        task.topology = None
        task.router = None
        task.gateways = {}

    def test_pchannel_uses_connect_timeout(self):
        """test _pchannel passes connect_timeout to gateway channel worker"""
        topofile = make_temp_file(
            dedent(
                """
                        [Main]
                        %s: dummy-gw
                        dummy-gw: dummy-node"""
                % HOSTNAME
            ).encode()
        )
        task = task_self()
        task.set_default("auto_tree", True)
        task.TOPOLOGY_CONFIGS = [topofile.name]

        # Set a specific connect_timeout
        task.set_info("connect_timeout", 42)

        # Patch the distant_worker to capture the timeout kwarg
        original_wrkcls = task.default("distant_worker")
        captured_kwargs = {}

        class SpyWorker(original_wrkcls):
            def __init__(self, *args, **kwargs):
                captured_kwargs.update(kwargs)
                super().__init__(*args, **kwargs)

        task.set_default("distant_worker", SpyWorker)

        try:
            # This triggers _pchannel to create a gateway channel worker.
            # It will fail with RouteResolvingError after the gateway is
            # marked unreachable, which is fine — we only care that the
            # worker was created with the right timeout.
            try:
                task.run("/bin/true", nodes="dummy-node", timeout=2)
            except RouteResolvingError:
                pass
        finally:
            task.set_default("distant_worker", original_wrkcls)

        # The gateway channel worker should have received timeout=42
        # (from connect_timeout), NOT None (the old broken behavior).
        self.assertEqual(
            captured_kwargs.get("timeout"),
            42,
            "Gateway channel worker should use connect_timeout as its timeout value",
        )

    def test_pchannel_timeout_none_when_connect_timeout_zero(self):
        """test _pchannel uses None when connect_timeout is 0 (unlimited)"""
        topofile = make_temp_file(
            dedent(
                """
                        [Main]
                        %s: dummy-gw
                        dummy-gw: dummy-node"""
                % HOSTNAME
            ).encode()
        )
        task = task_self()
        task.set_default("auto_tree", True)
        task.TOPOLOGY_CONFIGS = [topofile.name]

        # connect_timeout=0 means unlimited per ClusterShell convention
        task.set_info("connect_timeout", 0)

        original_wrkcls = task.default("distant_worker")
        captured_kwargs = {}

        class SpyWorker(original_wrkcls):
            def __init__(self, *args, **kwargs):
                captured_kwargs.update(kwargs)
                super().__init__(*args, **kwargs)

        task.set_default("distant_worker", SpyWorker)

        try:
            try:
                task.run("/bin/true", nodes="dummy-node", timeout=2)
            except RouteResolvingError:
                pass
        finally:
            task.set_default("distant_worker", original_wrkcls)

        # connect_timeout=0 should result in timeout=None (no timeout)
        # because `0 or None` evaluates to None
        self.assertIsNone(
            captured_kwargs.get("timeout"),
            "Gateway channel worker should have no timeout when connect_timeout is 0",
        )

    def test_propagation_channel_invalidates_timeout_on_start(self):
        """test PropagationChannel cancels timeout after StartMessage"""
        task = task_self()
        task.set_info("connect_timeout", 10)

        # Create a mock gateway string and a PropagationChannel
        gateway = NodeSet("mock-gw")
        chan = PropagationChannel(task, gateway)

        # Simulate a chanworker with a mock engine client
        mock_client = MagicMock()
        mock_client._engine_clients = MagicMock(return_value=[mock_client])

        mock_chanworker = MagicMock()
        mock_chanworker._engine_clients = MagicMock(return_value=[mock_client])

        # Register the mock gateway in task.gateways
        task.gateways[str(gateway)] = (mock_chanworker, set())

        # Mock the engine's timerq (save original for restoration)
        original_timerq = task._engine.timerq
        mock_timerq = MagicMock()
        task._engine.timerq = mock_timerq

        # Simulate receiving a StartMessage
        from ClusterShell.Communication import StartMessage

        start_msg = MagicMock()
        start_msg.type = StartMessage.ident

        # Need to set up the channel's _xml_reader mock
        chan._xml_reader = MagicMock()
        chan._xml_reader.version = "1.0"

        chan.recv(start_msg)

        # Verify the timer was invalidated
        self.assertTrue(chan.opened)
        mock_timerq.invalidate.assert_called_once_with(mock_client)

        # Restore timerq
        task._engine.timerq = original_timerq
        # Cleanup
        task.gateways = {}


if __name__ == "__main__":
    unittest.main()
