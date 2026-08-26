"""Pure realtime session state: sequences, response generations, dedup.

No network or database I/O lives here. The reducer tracks:

- client sequence monotonicity;
- the next server sequence;
- a monotonically increasing local ``response_generation``;
- cancelled generations whose late audio must be rejected;
- per-generation audio chunk sequence monotonicity.

``response_id(generation)`` only exposes local stable values such as
``response-1``; browser events never depend on unverified provider IDs.
"""

from __future__ import annotations

_BOUNDARY_EVENTS = frozenset({550, 350})


class RealtimeState:
    def __init__(self, *, session_id: str) -> None:
        self.session_id = session_id
        self._last_client_seq = 0
        self._next_server_seq = 1
        self._next_generation = 1
        self._active_generation: int | None = None
        self._cancelled: set[int] = set()
        self._closed: set[int] = set()
        self._last_chunk_seq: dict[int, int] = {}
        self._input_armed = True
        self._text_done: set[int] = set()
        self._audio_done: set[int] = set()

    # -- client / server sequences -------------------------------------------

    def accept_client_seq(self, seq: int) -> bool:
        if not isinstance(seq, int) or seq <= self._last_client_seq:
            return False
        self._last_client_seq = seq
        return True

    def next_server_seq(self) -> int:
        value = self._next_server_seq
        self._next_server_seq += 1
        return value

    # -- response generations --------------------------------------------------

    @staticmethod
    def response_id(generation: int) -> str:
        return f"response-{generation}"

    @property
    def active_generation(self) -> int | None:
        return self._active_generation

    def open_response_boundary(self, *, event: int) -> int | None:
        if event not in _BOUNDARY_EVENTS:
            return None
        if self._active_generation is not None:
            return self._active_generation
        if not self._input_armed:
            return None
        generation = self._next_generation
        self._next_generation += 1
        self._active_generation = generation
        self._input_armed = False
        return generation

    def accept_audio(self, *, generation: int, chunk_seq: int) -> bool:
        if generation != self._active_generation:
            return False
        if generation in self._cancelled or generation in self._closed:
            return False
        last = self._last_chunk_seq.get(generation, 0)
        if chunk_seq <= last:
            return False
        self._last_chunk_seq[generation] = chunk_seq
        return True

    def mark_text_done(self, generation: int) -> None:
        if generation == self._active_generation:
            self._text_done.add(generation)

    def text_done(self, generation: int) -> bool:
        return generation in self._text_done

    def audio_done(self, generation: int) -> bool:
        return generation in self._audio_done

    def close_response(self, *, generation: int, event: int) -> bool:
        if event != 359:
            return False
        if generation != self._active_generation:
            return False
        self._closed.add(generation)
        self._audio_done.add(generation)
        self._active_generation = None
        return True

    def interrupt_for_new_input(self) -> int | None:
        """Invalidate the active generation; block new boundaries until the
        next input turn completes. Returns the cancelled generation, if any."""
        generation = self._active_generation
        self._active_generation = None
        self._input_armed = False
        if generation is not None:
            self._cancelled.add(generation)
        return generation

    def complete_input_turn(self) -> None:
        self._input_armed = True

    def is_cancelled(self, generation: int) -> bool:
        return generation in self._cancelled
