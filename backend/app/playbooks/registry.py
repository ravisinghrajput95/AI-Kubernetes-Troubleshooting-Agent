from collections.abc import Iterable

from loguru import logger

from app.playbooks.base import Playbook, PlaybookContext


class PlaybookRegistry:
    def __init__(self, playbooks: Iterable[Playbook] | None = None) -> None:
        self._playbooks: dict[str, Playbook] = {}
        for playbook in playbooks or ():
            self.register(playbook)

    def register(self, playbook: Playbook) -> None:
        if not playbook.id:
            raise ValueError(f"{type(playbook).__name__} has no id")
        if playbook.id in self._playbooks:
            raise ValueError(f"Duplicate playbook id: {playbook.id}")
        self._playbooks[playbook.id] = playbook

    @property
    def playbooks(self) -> list[Playbook]:
        return list(self._playbooks.values())

    def select(self, context: PlaybookContext) -> list[Playbook]:
        """Playbooks whose triggers appear in the current analysis.

        A playbook raising during selection is skipped rather than aborting the
        investigation, matching the isolation applied to collectors and rules.
        """
        selected = []

        for playbook in self._playbooks.values():
            try:
                if playbook.applicable(context):
                    selected.append(playbook)
            except Exception as exc:
                logger.opt(exception=exc).error(
                    "Playbook {id} failed during selection", id=playbook.id
                )

        return selected
