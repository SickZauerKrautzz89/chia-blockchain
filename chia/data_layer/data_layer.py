import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import aiosqlite
from chia.daemon.keychain_proxy import KeychainProxyConnectionFailure
from chia.data_layer.data_layer_types import InternalNode, TerminalNode
from chia.data_layer.data_layer_wallet import DataLayerWallet
from chia.data_layer.data_store import DataStore
from chia.server.server import ChiaServer
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.util.config import load_config
from chia.util.db_wrapper import DBWrapper
from chia.util.path import mkdir, path_from_root
from chia.wallet.wallet_node import WalletNode


class DataLayer:
    data_store: DataStore
    db_wrapper: DBWrapper
    db_path: Path
    connection: Optional[aiosqlite.Connection]
    config: Dict[str, Any]
    log: logging.Logger
    wallet: DataLayerWallet
    wallet_node: Optional[WalletNode]
    state_changed_callback: Optional[Callable[..., object]]
    initialized: bool

    def __init__(
        self,
        root_path: Path,
        wallet_node: WalletNode,
        name: Optional[str] = None,
    ):
        if name == "":
            # TODO: If no code depends on "" counting as 'unspecified' then we do not
            #       need this.
            name = None
        config = load_config(root_path, "config.yaml", "data_layer")
        self.initialized = False
        self.config = config
        self.connection = None
        self.wallet_node = wallet_node
        self.log = logging.getLogger(name if name is None else __name__)
        db_path_replaced: str = config["database_path"].replace("CHALLENGE", config["selected_network"])
        self.db_path = path_from_root(root_path, db_path_replaced)
        mkdir(self.db_path.parent)

    def _set_state_changed_callback(self, callback: Callable[..., object]) -> None:
        self.state_changed_callback = callback

    def set_server(self, server: ChiaServer) -> None:
        self.server = server

    async def _start(self) -> bool:
        # create the store (db) and data store instance
        assert self.wallet_node
        try:
            private_key = await self.wallet_node.get_key_for_fingerprint(None)
        except KeychainProxyConnectionFailure:
            self.log.error("Failed to connect to keychain service")
            return False

        if private_key is None:
            self.logged_in = False
            return False
        self.connection = await aiosqlite.connect(self.db_path)
        self.db_wrapper = DBWrapper(self.connection)
        self.data_store = await DataStore.create(self.db_wrapper)
        assert self.wallet_node.wallet_state_manager
        main_wallet = self.wallet_node.wallet_state_manager.main_wallet
        async with self.wallet_node.wallet_state_manager.lock:
            creation_record = await self.wallet.create_new_dl_wallet(
                self.wallet_node.wallet_state_manager, main_wallet, None
            )
            self.wallet = creation_record.item
        self.initialized = True
        return True

    def _close(self) -> None:
        # TODO: review for anything else we need to do here
        # self._shut_down = True
        pass

    async def _await_closed(self) -> None:
        if self.connection is not None:
            await self.connection.close()

    async def create_store(self) -> bytes32:
        assert self.wallet.dl_info.origin_coin
        tree_id = self.wallet.dl_info.origin_coin.name()
        res = await self.data_store.create_tree(tree_id)
        if res is None:
            self.log.fatal("failed creating store")
        return tree_id

    async def insert(
        self,
        tree_id: bytes32,
        changelist: List[Dict[str, Any]],
    ) -> bool:
        for change in changelist:
            if change["action"] == "insert":
                key = change["key"]
                value = change["value"]
                reference_node_hash = change.get("reference_node_hash")
                side = change.get("side")
                if reference_node_hash or side:
                    await self.data_store.insert(key, value, tree_id, reference_node_hash, side)
                await self.data_store.autoinsert(key, value, tree_id)
            else:
                assert change["action"] == "delete"
                key = change["key"]
                await self.data_store.delete(key, tree_id)

        root = await self.data_store.get_tree_root(tree_id)
        assert root.node_hash
        res = await self.wallet.create_update_state_spend(root.node_hash)
        assert res
        # todo need to mark data as pending and change once tx is confirmed
        return True

    async def get_value(self, store_id: bytes32, key: bytes) -> bytes:
        res = await self.data_store.get_node_by_key(tree_id=store_id, key=key)
        if res is None:
            self.log.error("Failed to create tree")
        return res.value

    async def get_pairs(self, store_id: bytes32) -> List[TerminalNode]:
        res = await self.data_store.get_pairs(store_id)
        if res is None:
            self.log.error("Failed to create tree")
        return res

    async def get_ancestors(self, node_hash: bytes32, store_id: bytes32) -> List[InternalNode]:
        res = await self.data_store.get_ancestors(store_id, node_hash)
        if res is None:
            self.log.error("Failed to create tree")
        return res
