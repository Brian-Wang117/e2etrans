"""State machine invariants: sequences, generations, interruption."""

from app.realtime.state import RealtimeState


def make_state():
    return RealtimeState(session_id="sess-1")


def test_client_sequence_must_strictly_increase():
    state = make_state()
    assert state.accept_client_seq(1) is True
    assert state.accept_client_seq(1) is False
    assert state.accept_client_seq(0) is False
    assert state.accept_client_seq(2) is True
    assert state.accept_client_seq(True) is False


def test_server_sequence_is_monotonic():
    state = make_state()
    assert state.next_server_seq() == 1
    assert state.next_server_seq() == 2


def test_response_boundary_opens_once_per_input_turn():
    state = make_state()
    first = state.open_response_boundary(event=550)
    assert first == 1
    # Same logical response: both 550 and 350 map onto the active generation.
    assert state.open_response_boundary(event=350) == 1
    # Input is disarmed until the user turn completes.
    state.close_response(generation=1, event=359)
    assert state.open_response_boundary(event=550) is None
    state.complete_input_turn()
    assert state.open_response_boundary(event=350) == 2


def test_non_boundary_events_return_none():
    state = make_state()
    assert state.open_response_boundary(event=352) is None


def test_audio_chunks_are_monotonic_per_generation():
    state = make_state()
    generation = state.open_response_boundary(event=350)
    assert state.accept_audio(generation=generation, chunk_seq=1) is True
    assert state.accept_audio(generation=generation, chunk_seq=1) is False
    assert state.accept_audio(generation=generation, chunk_seq=2) is True
    assert state.accept_audio(generation=generation + 1, chunk_seq=3) is False


def test_interrupt_cancels_active_generation_and_blocks_boundaries():
    state = make_state()
    generation = state.open_response_boundary(event=550)
    cancelled = state.interrupt_for_new_input()
    assert cancelled == generation
    assert state.is_cancelled(generation) is True
    assert state.active_generation is None
    # Late audio for the cancelled response is rejected even after re-arming.
    state.complete_input_turn()
    assert state.accept_audio(generation=generation, chunk_seq=1) is False
    assert state.open_response_boundary(event=550) == generation + 1


def test_close_response_requires_event_359_and_active_generation():
    state = make_state()
    generation = state.open_response_boundary(event=550)
    assert state.close_response(generation=generation, event=352) is False
    assert state.close_response(generation=generation + 1, event=359) is False
    assert state.close_response(generation=generation, event=359) is True
    assert state.audio_done(generation) is True


def test_response_id_is_stable_and_local():
    assert RealtimeState.response_id(3) == "response-3"
