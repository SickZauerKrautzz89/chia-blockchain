import asyncio
from chia.util.config import load_config, save_config
import logging
from pathlib import Path

import pytest

from chia.consensus.block_rewards import calculate_base_farmer_reward, calculate_pool_reward
from chia.rpc.full_node_rpc_api import FullNodeRpcApi
from chia.rpc.full_node_rpc_client import FullNodeRpcClient
from chia.rpc.rpc_server import start_rpc_server
from chia.rpc.wallet_rpc_api import WalletRpcApi
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.simulator.simulator_protocol import FarmNewBlockProtocol
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.peer_info import PeerInfo
from chia.util.bech32m import encode_puzzle_hash
from chia.consensus.coinbase import create_puzzlehash_for_pk
from chia.wallet.derive_keys import master_sk_to_wallet_sk
from chia.util.ints import uint16, uint32, uint64
from chia.wallet.transaction_record import TransactionRecord
from tests.setup_nodes import bt, setup_simulators_and_wallets, self_hostname
from tests.time_out_assert import time_out_assert
from tests.util.rpc import validate_get_routes

log = logging.getLogger(__name__)


class TestWalletRpc:
    @pytest.fixture(scope="function")
    async def two_wallet_nodes(self):
        async for _ in setup_simulators_and_wallets(1, 2, {}):
            yield _

    @pytest.mark.asyncio
    async def test_wallet_make_transaction(self, two_wallet_nodes):
        test_rpc_port = uint16(21529)
        test_rpc_port_node = uint16(21530)
        num_blocks = 5
        full_nodes, wallets = two_wallet_nodes
        full_node_api = full_nodes[0]
        full_node_server = full_node_api.full_node.server
        wallet_node, server_2 = wallets[0]
        wallet_node_2, server_3 = wallets[1]
        wallet = wallet_node.wallet_state_manager.main_wallet
        wallet_2 = wallet_node_2.wallet_state_manager.main_wallet
        ph = await wallet.get_new_puzzlehash()
        ph_2 = await wallet_2.get_new_puzzlehash()

        await server_2.start_client(PeerInfo("localhost", uint16(full_node_server._port)), None)

        for i in range(0, num_blocks):
            await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))

        initial_funds = sum(
            [calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)]
        )
        initial_funds_eventually = sum(
            [
                calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i))
                for i in range(1, num_blocks + 1)
            ]
        )

        wallet_rpc_api = WalletRpcApi(wallet_node)

        config = bt.config
        hostname = config["self_hostname"]
        daemon_port = config["daemon_port"]

        def stop_node_cb():
            pass

        full_node_rpc_api = FullNodeRpcApi(full_node_api.full_node)

        rpc_cleanup_node = await start_rpc_server(
            full_node_rpc_api,
            hostname,
            daemon_port,
            test_rpc_port_node,
            stop_node_cb,
            bt.root_path,
            config,
            connect_to_daemon=False,
        )
        rpc_cleanup = await start_rpc_server(
            wallet_rpc_api,
            hostname,
            daemon_port,
            test_rpc_port,
            stop_node_cb,
            bt.root_path,
            config,
            connect_to_daemon=False,
        )

        await time_out_assert(5, wallet.get_confirmed_balance, initial_funds)
        await time_out_assert(5, wallet.get_unconfirmed_balance, initial_funds)

        client = await WalletRpcClient.create(self_hostname, test_rpc_port, bt.root_path, config)
        await validate_get_routes(client, wallet_rpc_api)

        try:
            merkle_root: bytes32 = bytes32([0] * 32)
            dl_wallet_id, txs = await client.create_new_dl_wallet(merkle_root, uint64(50))
            assert len(txs) == 2
            current_root: bytes32 = await client.dl_current_root(dl_wallet_id)
            assert current_root == merkle_root
            new_root: bytes32 = bytes32([1] * 32)
            tx_record: TransactionRecord = await client.dl_update_root(dl_wallet_id, new_root)
            assert tx_record

            for i in range(0, 5):
                await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(bytes32([0] * 32)))

            time_out_assert(15, client.dl_current_root, new_root, dl_wallet_id)
        finally:
            # Checks that the RPC manages to stop the node
            client.close()
            await client.await_closed()
            await rpc_cleanup()
            await rpc_cleanup_node()