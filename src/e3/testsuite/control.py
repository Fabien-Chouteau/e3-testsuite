from enum import Enum


class TestControlKind(Enum):
    """Control how to run (or not!) testcases."""

    NONE = 0
    """Run the test the regular way."""

    SKIP = 1
    """Do not run the testcase, setting it UNSUPPORTED."""

    XFAIL = 2
    """
    Run the test the regular way. If its status is PASS, correct it to
    XPASS. If it succeeds, correct it to XFAIL. Leave its status unchanged in
    other cases.
    """


class TestControl(object):
    """Control the execution and analysis of a testcase."""

    def __init__(self, message=None, skip=False, xfail=False):
        """Initialize a TestControl instance.

        :param None|str message: Optional message to convey with the test
            status.
        :param bool skip: Whether to skip the test execution.
        :param bool xfail: Whether we expect the test to fail. If the test
            should be skipped, and xfailed, we consider it failed even though
            it did not run.
        """
        self.skip = skip
        self.xfail = xfail
        self.message = message

    @classmethod
    def interpret(cls, driver, condition_env={}):
        """Interpret the test "control" configuration in ``test_env``.

        Raise a ValueError exception if the configuration is invalid.

        :param TestDriver driver: Test driver for which we must parse the
            "control" configuration.
        :param dict condition_env: Environment to pass to condition evaluation
            in control entries.
        """
        # Read the configuration from the test environment's "control" key, if
        # present.
        default = cls()
        try:
            control = driver.test_env["control"]
        except KeyError:
            return default

        # Variables available to entry conditions
        condition_env = dict(condition_env)
        condition_env["env"] = driver.env

        # First validate the whole control structure, and only then interpret
        # it, for the same reason an language interpreter checks the syntax
        # before starting the interpretation.
        #
        # We expect control to be a list of lists of strings. The top-level
        # list is a collection of "entries": each entry conditionally selects a
        # test behavior. Each entry (list of strings) have one of the following
        # format:
        #
        #    [kind, condition]
        #    [kind, condition, message]
        #
        # "kind" is the name of any of the TestControlKind values. "condition"
        # is a Python expression that determines whether the entry applies, and
        # the optional "message" is a free form text to track which entry was
        # selected.
        entries = []

        if not isinstance(control, list):
            raise ValueError("list expected at the top level")

        for i, entry in enumerate(control, 1):
            def error(message):
                raise ValueError("entry #{}: {}".format(i, message))

            if (
                not isinstance(entry, list) or
                not len(entry) in (2, 3) or
                any(not isinstance(s, str) for s in entry)
            ):
                error("list of 2 or 3 strings expected")

            # Decode the test control kind
            try:
                kind = TestControlKind[entry[0]]
            except KeyError:
                error("invalid kind: {}".format(entry[0]))

            # Evaluate the condition
            try:
                cond = eval(entry[1], condition_env)
            except Exception as exc:
                error("invalid condition ({}): {}"
                      .format(type(exc).__name__, exc))

            message = entry[2] if len(entry) > 2 else None

            entries.append((kind, cond, message))

        # Now, select the first entry whose condition is True. By default,
        # fallback to "default".
        for kind, cond, message in entries:
            if cond:
                skip, xfail = {
                    TestControlKind.NONE: (False, False),
                    TestControlKind.SKIP: (True, False),
                    TestControlKind.XFAIL: (False, True),
                }[kind]
                return TestControl(message, skip, xfail)
        return default