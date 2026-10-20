"""
Party speakers: a main player plays, and the chosen other speakers join it.

A party speaker plays through a normal MusicAssistantBackend on the main
player's MA queue. Right before the party starts playing, the members are
grouped with the main player, by Music Assistant or by Home Assistant. The
members the party added leave again once the music has been stopped for a
while (``after_party``), or at once when the Qobuz app leaves the speaker
(``on_leave``). Grouping never keeps the music from starting: what went wrong
is logged and shown in the panel.
"""

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from music_assistant_models.enums import PlaybackState as MusicAssistantState
from music_assistant_models.errors import MusicAssistantError

from qobuz_proxy.backends.types import BackendTrackMetadata, PlaybackState

from .backend import MusicAssistantBackend
from .connection import MusicAssistantConnection, NotConnectedError
from .home_assistant import HomeAssistant, HomeAssistantError, MediaPlayer
from .settings import PartySpeaker

logger = logging.getLogger(__name__)

# A track change is a stop and then a play: only a longer stop ends the party
PARTY_RELEASE_DELAY_SECONDS = 5.0


def _names(names: list[str]) -> str:
    """'A', 'A and B', 'A, B and C'."""
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _sentence(text: str) -> str:
    return text.strip().rstrip(".") + "."


class Party:
    """Groups a party speaker's members with its main player, and splits them again."""

    def __init__(
        self,
        party_speaker: PartySpeaker,
        connection: MusicAssistantConnection,
        home_assistant: HomeAssistant,
        name: str,
    ) -> None:
        self.party_speaker = party_speaker
        self.problem = ""  # what went wrong with the grouping, for the panel
        self._connection = connection
        self._home_assistant = home_assistant
        self._name = name
        self._is_gathered = False
        self._added: dict[str, str] = {}  # id -> name of the members it grouped and not split
        self._lock = asyncio.Lock()
        self._release_timer: Optional[asyncio.TimerHandle] = None
        self._is_release_due = False
        self._tasks: set[asyncio.Future[Any]] = set()

    async def gather(self) -> None:
        """Group the members with the main player, unless the party is gathered already.

        A play also keeps a stopped party from ending.
        """
        self._cancel_release()
        async with self._lock:
            if self._is_gathered:
                return
            self._is_gathered = True
            if self.party_speaker.grouping == "home_assistant":
                problems = await self._gather_in_home_assistant()
            else:
                problems = await self._gather_in_music_assistant()
            for problem in problems:
                logger.warning("[%s] %s", self._name, problem)
            self.problem = " ".join(problems)

    def on_state(self, state: PlaybackState) -> None:
        """A stop that lasts PARTY_RELEASE_DELAY_SECONDS ends the party (``after_party``)."""
        if (
            state == PlaybackState.STOPPED
            and self.party_speaker.after_party == "split"
            and self._is_gathered
            and self._release_timer is None
        ):
            self._is_release_due = True
            self._release_timer = asyncio.get_running_loop().call_later(
                PARTY_RELEASE_DELAY_SECONDS, self._on_release_timer
            )

    def leave(self) -> None:
        """The Qobuz app left the party speaker: split at once (``on_leave``), or keep it."""
        self._cancel_release()
        if self.party_speaker.on_leave == "split" and self._is_gathered:
            self._is_release_due = True
            self._run_in_background(self._release())

    async def close(self) -> None:
        """The speaker stops: a split that was due happens now."""
        if self._release_timer is not None:
            self._release_timer.cancel()
            self._release_timer = None
            await self._release()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _gather_in_music_assistant(self) -> list[str]:
        party = self.party_speaker
        main_id = party.main_player_id
        main = self._connection.player(main_id)
        if main is None or not main.available:
            # Music Assistant's own message would name the player by its id
            name = self._player_name(main_id) if main is not None else "The main speaker"
            return [f"The speakers could not join: {name} is not available in Music Assistant."]
        with_main = set(main.group_members)
        problems, to_add = [], []
        for member_id in party.member_ids:
            member = self._connection.player(member_id)
            name = self._player_name(member_id)
            if member_id in with_main or (member is not None and member.synced_to == main_id):
                continue  # with the main already
            if member is None or not member.available:
                problems.append(f"{name} could not join: it is not available in Music Assistant.")
            elif main.can_group_with and member_id not in main.can_group_with:
                problems.append(
                    f"{name} could not join: it can't sync with {self._player_name(main_id)}."
                )
            elif (
                party.busy_speakers == "skip"
                and member.playback_state == MusicAssistantState.PLAYING
            ):
                logger.info("[%s] %s plays something else; leaving it playing", self._name, name)
            else:
                to_add.append(member_id)
        if not to_add:
            return problems
        joining = {member_id: self._player_name(member_id) for member_id in to_add}
        names = _names(list(joining.values()))
        try:
            await self._connection.call(
                f"players/cmd/set_members target={main_id} add={','.join(to_add)}",
                lambda c: c.players.set_members(main_id, player_ids_to_add=to_add),
            )
        except (NotConnectedError, MusicAssistantError) as e:
            problems.append(f"{names} could not join: {_sentence(str(e))}")
        else:
            self._added.update(joining)
            logger.info("[%s] %s joined %s", self._name, names, self._player_name(main_id))
        return problems

    async def _gather_in_home_assistant(self) -> list[str]:
        party = self.party_speaker
        try:
            players = {p.entity_id: p for p in await self._home_assistant.media_players()}
        except HomeAssistantError as e:
            return [f"The speakers could not join: {_sentence(str(e))}"]
        main = players.get(party.main_entity_id)
        if main is None:
            return [f"The speakers could not join: Home Assistant has no {party.main_entity_id}."]
        problems, to_add = [], []
        for entity_id in party.member_ids:
            member: Optional[MediaPlayer] = players.get(entity_id)
            if entity_id in main.group_members:
                continue  # with the main already
            if member is None or member.state == "unavailable":
                problems.append(
                    f"{member.name if member else entity_id} could not join: it is not "
                    "available in Home Assistant."
                )
            elif party.busy_speakers == "skip" and member.state == "playing":
                logger.info(
                    "[%s] %s plays something else; leaving it playing", self._name, member.name
                )
            else:
                to_add.append(entity_id)
        if not to_add:
            return problems
        joining = {entity_id: players[entity_id].name for entity_id in to_add}
        names = _names(list(joining.values()))
        try:
            await self._home_assistant.join(main.entity_id, to_add)
        except HomeAssistantError as e:
            problems.append(f"{names} could not join: {_sentence(str(e))}")
        else:
            self._added.update(joining)
            logger.info("[%s] %s joined %s in Home Assistant", self._name, names, main.name)
        return problems

    async def _release(self) -> None:
        """The members this party added leave the main player's group."""
        async with self._lock:
            if not self._is_release_due:
                return  # the party plays again
            self._is_release_due = False
            self._is_gathered = False
            if not self._added:
                return
            if self.party_speaker.grouping == "home_assistant":
                await self._release_in_home_assistant()
            else:
                await self._release_in_music_assistant()

    async def _release_in_music_assistant(self) -> None:
        main_id = self.party_speaker.main_player_id
        added = list(self._added)
        names = _names(list(self._added.values()))
        try:
            await self._connection.call(
                f"players/cmd/set_members target={main_id} remove={','.join(added)}",
                lambda c: c.players.set_members(main_id, player_ids_to_remove=added),
            )
        except (NotConnectedError, MusicAssistantError) as e:
            self._report(f"{names} could not leave the party: {_sentence(str(e))}")
            return
        self._added.clear()
        logger.info("[%s] %s left %s again", self._name, names, self._player_name(main_id))

    async def _release_in_home_assistant(self) -> None:
        async def unjoin(entity_id: str, name: str) -> str:
            try:
                await self._home_assistant.unjoin(entity_id)
            except HomeAssistantError as e:
                return f"{name} could not leave the party: {_sentence(str(e))}"
            del self._added[entity_id]
            logger.info("[%s] %s left the party in Home Assistant", self._name, name)
            return ""

        # One at a time could take 10 s each while Home Assistant does not answer
        problems = await asyncio.gather(*(unjoin(*member) for member in list(self._added.items())))
        if any(problems):
            self._report(" ".join(problem for problem in problems if problem))

    def _report(self, problem: str) -> None:
        logger.warning("[%s] %s", self._name, problem)
        self.problem = problem

    def _player_name(self, player_id: str) -> str:
        player = self._connection.player(player_id)
        return (player.name if player is not None else "") or player_id

    def _cancel_release(self) -> None:
        self._is_release_due = False
        if self._release_timer is not None:
            self._release_timer.cancel()
            self._release_timer = None

    def _on_release_timer(self) -> None:
        self._release_timer = None
        self._run_in_background(self._release())

    def _run_in_background(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class PartyBackend(MusicAssistantBackend):
    """A backend on the main player's MA queue that gathers the party before it plays."""

    def __init__(
        self,
        connection: MusicAssistantConnection,
        party: Party,
        name: str,
        session: Optional[dict[str, Any]] = None,
        save_session: Callable[[Optional[dict[str, Any]]], None] = lambda session: None,
    ) -> None:
        super().__init__(
            connection, party.party_speaker.main_player_id, name, session, save_session
        )
        self.party = party

    async def play(self, url: str, metadata: BackendTrackMetadata) -> None:
        if self._connection.is_connected and not self._has_external_playback:
            await self.party.gather()
        try:
            await super().play(url, metadata)
        except BaseException:
            self.party.on_state(self._state)  # nothing plays now: a stopped party still ends
            raise

    def app_left(self) -> None:
        """The Qobuz app left this speaker (another device picked, or disconnected)."""
        self.party.leave()

    async def disconnect(self) -> None:
        await super().disconnect()
        await self.party.close()

    def _notify_state_change(self, state: PlaybackState) -> None:
        super()._notify_state_change(state)
        self.party.on_state(state)
