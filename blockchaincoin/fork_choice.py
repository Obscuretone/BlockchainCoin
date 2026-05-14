"""Fork-choice validation for the UTXO consensus node.

The fork store can contain multiple branches. This module selects the best tip
from storage, replays that branch through the consensus engine, and returns the
resulting active state. The replay step is deliberate: stored metadata and
indexes never define validity by themselves.
"""

from __future__ import annotations

from dataclasses import dataclass

from .consensus import ConsensusBlock, ConsensusError, ConsensusState, rebuild_state_from_blocks
from .constants import MAX_MONEY
from .storage import SQLiteForkStore, StorageError, StoredConsensusBlock


class ForkChoiceError(RuntimeError):
    """Raised when stored fork data cannot produce a valid active chain."""

    pass


@dataclass(frozen=True)
class ActiveChain:
    """A validated branch, its replayed state, and cumulative work."""

    blocks: tuple[ConsensusBlock, ...]
    state: ConsensusState
    cumulative_work: int

    @property
    def tip(self) -> ConsensusBlock | None:
        return self.blocks[-1] if self.blocks else None


class ForkChoice:
    """Selects and validates the active chain from fork-aware block storage."""

    def __init__(
        self,
        store: SQLiteForkStore,
        block_subsidy: int = 50,
        max_money: int = MAX_MONEY,
    ) -> None:
        self.store = store
        self.block_subsidy = block_subsidy
        self.max_money = max_money

    def add_block(self, block: ConsensusBlock) -> ActiveChain:
        """Store a block and return the resulting active branch."""

        self.store.put_block(block)
        return self.active_chain()

    def active_chain(self) -> ActiveChain:
        """Return the best stored branch after consensus replay."""

        try:
            stored_chain = self.store.active_chain()
        except StorageError as exc:
            raise ForkChoiceError("active branch is disconnected") from exc
        if not stored_chain:
            return ActiveChain(
                blocks=(),
                state=ConsensusState(
                    block_subsidy=self.block_subsidy,
                    max_money=self.max_money,
                ),
                cumulative_work=0,
            )
        return self._validate_stored_chain(stored_chain)

    def chain_to_tip(self, tip_hash: str) -> ActiveChain:
        """Return a validated branch ending at an explicit tip hash."""

        return self._validate_stored_chain(self.store.chain_to_tip(tip_hash))

    def best_tip(self) -> StoredConsensusBlock | None:
        return self.store.best_tip()

    def _validate_stored_chain(
        self,
        stored_chain: list[StoredConsensusBlock],
    ) -> ActiveChain:
        try:
            state = rebuild_state_from_blocks(
                [stored.block for stored in stored_chain],
                block_subsidy=self.block_subsidy,
                max_money=self.max_money,
            )
        except ConsensusError as exc:
            raise ForkChoiceError("stored branch failed consensus validation") from exc
        return ActiveChain(
            blocks=tuple(stored.block for stored in stored_chain),
            state=state,
            cumulative_work=stored_chain[-1].cumulative_work,
        )

    def validate_all_known_branches(self) -> None:
        """Replay every known branch tip to detect disconnected or invalid data."""

        for stored in self.store.iter_blocks():
            try:
                self.chain_to_tip(stored.block.hash)
            except StorageError as exc:
                raise ForkChoiceError("stored branch is disconnected") from exc
