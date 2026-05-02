from knife.core.policy import Policy, PolicyAction, PolicyMode


def test_policy_off_never_violates():
    p = Policy(mode=PolicyMode.OFF, block=["chrome"])
    assert p.is_violation("chrome") is False


def test_policy_block_list_matches_substring():
    p = Policy(mode=PolicyMode.BLOCK_LIST, block=["chrome"])
    assert p.is_violation("Google Chrome Helper") is True
    assert p.is_violation("firefox") is False


def test_policy_allow_list_blocks_others():
    p = Policy(mode=PolicyMode.ALLOW_LIST, allow=["python"])
    assert p.is_violation("python3.12") is False
    assert p.is_violation("chrome") is True


def test_policy_protected_never_violates():
    p = Policy(mode=PolicyMode.BLOCK_LIST, block=["*"], protected=["launchd"])
    assert p.is_violation("launchd") is False
    assert p.is_violation("anything-else") is True


def test_policy_round_trip_dict():
    p = Policy(mode=PolicyMode.ALLOW_LIST, action=PolicyAction.SUSPEND,
               allow=["a", "b"], block=["x"])
    d = p.to_dict()
    q = Policy.from_dict(d)
    assert q.mode == p.mode
    assert q.action == p.action
    assert q.allow == p.allow
    assert q.block == p.block
