import shioaji_client as sc


def test_wrapped_session_failure_is_recognized():
    try:
        raise RuntimeError("code: NotReady, SubCode(SessionNotEstablished)")
    except RuntimeError as cause:
        try:
            raise sc.DataUnavailable("kbars failed") from cause
        except sc.DataUnavailable as wrapped:
            assert sc.is_session_unavailable(wrapped)


def test_unrelated_failure_is_not_a_session_failure():
    assert not sc.is_session_unavailable(sc.DataUnavailable("database unavailable"))
