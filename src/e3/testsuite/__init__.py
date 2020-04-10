from e3.collection.dag import DAG
from e3.env import Env, BaseEnv
from e3.fs import find, rm, mkdir, mv
import e3.log
from e3.job import Job
from e3.job.scheduler import Scheduler
from e3.main import Main
from e3.os.process import quote_arg
from e3.testsuite.driver import TestDriver
from e3.testsuite.report.xunit import dump_xunit_report
from e3.testsuite.result import TestResult, TestStatus
from e3.yaml import load_with_config

import collections.abc
import traceback
import logging
import os
import yaml
import sys
import re
import tempfile

from colorama import Fore, Style

logger = logging.getLogger("testsuite")


class TooManyErrors(Exception):
    pass


class TestAbort(Exception):
    pass


def isatty(stream):
    """Return whether stream is a TTY.

    This is a safe predicate: it works if stream is None or if it does not even
    support TTY detection: in these cases, be conservative (consider it's not a
    TTY).
    """
    return stream and getattr(stream, 'isatty') and stream.isatty()


class DummyColors(object):
    """
    Stub object to replace colorama's Fore/Style classes when colors are
    disabled.
    """
    def __getattr__(self, name):
        return ''


class TestFragment(Job):
    """Job used in a testsuite.

    :ivar test_instance: a TestDriver instance
    :ivar data: a function to call with the following signature (,) -> None
    """

    def __init__(self, uid, test_instance, fun, previous_values, notify_end):
        """Initialize a TestFragment.

        :param uid: uid of the test fragment (should be unique)
        :type uid: str
        :param test_instance: a TestDriver instance
        :type test_instance: e3.testsuite.driver.TestDriver
        :param fun: callable to be executed by the job
        :type fun: (,) -> None
        :param notify_end: Internal parameter. See e3.job
        :type notify_end: str -> None
        """
        super(TestFragment, self).__init__(uid, fun, notify_end)
        self.test_instance = test_instance
        self.previous_values = previous_values

    def run(self):
        """Run the test fragment."""
        self.return_value = None
        try:
            self.return_value = self.data(self.previous_values)
        except TestAbort:
            pass
        except Exception as e:
            # In case of exception generate a test result. The name is based
            # on the test name with an additional random part to avoid
            # conflicts
            logger.exception("got exception in test: %s", e)
            test = self.test_instance
            test.push_result(
                TestResult(
                    "%s__except%s" % (test.test_name, self.index),
                    env=test.test_env,
                    status=TestStatus.ERROR,
                )
            )
            self.return_value = e


class TestsuiteCore(object):
    """Testsuite Core driver.

    This class is the base of Testsuite class and should not be instanciated.
    It's not recommended to override any of the functions declared in it.

    See documentation of Testsuite class for overridable methods and
    variables.
    """

    def __init__(self, root_dir, testsuite_name="Untitled testsute"):
        """Testsuite constructor.

        :param root_dir: root dir of the testsuite. Usually the directory in
            which testsuite.py and runtest.py are located
        :param str testsuite_name: Name for this testsuite. It can be used to
            provide a title in some report formats.
        :type root_dir: str | unicode
        """
        self.root_dir = os.path.abspath(root_dir)
        self.test_dir = os.path.join(self.root_dir, self.TEST_SUBDIR)
        self.consecutive_failures = 0
        self.return_values = {}
        self.results = {}
        self.result_tracebacks = {}
        self.test_counter = 0
        self.test_status_counters = {s: 0 for s in TestStatus}
        self.testsuite_name = testsuite_name

    def test_result_filename(self, test_name):
        """Return the name of the file in which the result are stored.

        :param test_case_file: path to a test case scenario relative to the
            test directory
        :type test_case_file: str | unicode
        :param variant: the test variant
        :type variant: str
        :return: the test name. Note that test names should not contain path
            separators
        :rtype: str | unicode
        """
        return os.path.join(self.output_dir, test_name + ".yaml")

    def job_factory(self, uid, data, predecessors, notify_end):
        """Run internal function.

        See e3.job.scheduler
        """
        # we assume that data[0] is the test instance and data[1] the method
        # to call

        # When passing return values from predecessors, remove current test
        # name from the keys to ease referencing by user (the short fragment
        # name can then be used by user without knowing the full node id).
        key_prefix = data[0].test_name + "."
        key_prefix_len = len(key_prefix)

        def filter_key(k):
            if k.startswith(key_prefix):
                return k[key_prefix_len:]
            else:
                return k

        return TestFragment(
            uid,
            data[0],
            data[1],
            {filter_key(k): self.return_values[k] for k in predecessors},
            notify_end,
        )

    def testsuite_main(self, args=None):
        """Main for the main testsuite script.

        :param args: command line arguments. If None use sys.argv
        :type args: list[str] | None
        """
        self.main = Main(platform_args=self.CROSS_SUPPORT)

        # Add common options
        parser = self.main.argument_parser
        parser.add_argument(
            "-o",
            "--output-dir",
            metavar="DIR",
            default="./out",
            help="select output dir",
        )
        parser.add_argument("-t", "--temp-dir", metavar="DIR",
                            default=Env().tmp_dir)
        parser.add_argument(
            "-d", "--dev-temp",
            nargs="?", default=None, const="tmp",
            help="Unlike --temp-dir, use this very directory to store"
                 " testsuite temporaries (i.e. no random subdirectory). Also"
                 " automatically disable temp dir cleanup, to be developer"
                 " friendly. If no directory is provided, use the local"
                 " \"tmp\" directory")
        parser.add_argument(
            "--max-consecutive-failures",
            default=0,
            help="If there are more than N consecutive failures, the testsuite"
            " is aborted. If set to 0 (default) then the testsuite will never"
            " be stopped",
        )
        parser.add_argument(
            "--keep-old-output-dir",
            default=False,
            action="store_true",
            help="This is default with this testsuite framework. The option"
            " is kept only to keep backward compatibility of invocation with"
            " former framework (gnatpython.testdriver)",
        )
        parser.add_argument(
            "--disable-cleanup",
            dest="enable_cleanup",
            action="store_false",
            default=True,
            help="disable cleanup of working space",
        )
        parser.add_argument(
            "-j",
            "--jobs",
            dest="jobs",
            type=int,
            metavar="N",
            default=Env().build.cpu.cores,
            help="Specify the number of jobs to run simultaneously",
        )
        parser.add_argument(
            "--show-error-output",
            "-E",
            action="store_true",
            help="When testcases fail, display their output. This is for"
            " convenience for interactive use.",
        )
        parser.add_argument(
            "--dump-environ",
            dest="dump_environ",
            action="store_true",
            default=False,
            help="Dump all environment variables in a file named environ.sh,"
            " located in the output directory (see --output-dir). This"
            " file can then be sourced from a Bourne shell to recreate"
            " the environement that existed when this testsuite was run"
            " to produce a given testsuite report.",
        )
        parser.add_argument(
            "--xunit-output",
            dest="xunit_output",
            metavar="FILE",
            help="Output testsuite report to the given file in the standard"
            " XUnit XML format. This is useful to display results in"
            " continuous build systems such as Jenkins.",
        )
        parser.add_argument(
            "sublist", metavar="tests", nargs="*", default=[], help="test"
        )
        # Add user defined options
        self.add_options()

        # parse options
        self.main.parse_args(args)

        # If there is a chance for the logging to end up in a non-tty stream,
        # disable colors.
        self.Fore = Fore
        self.Style = Style
        enable_colors = True
        if (
            self.main.args.log_file or
            not isatty(sys.stdout) or
            not isatty(sys.stderr)
        ):
            enable_colors = False
            self.Fore = DummyColors()
            self.Style = DummyColors()

        self.env = BaseEnv.from_env()
        self.env.enable_colors = enable_colors
        self.env.root_dir = self.root_dir
        self.env.test_dir = self.test_dir

        # At this stage compute commonly used paths
        # Keep the working dir as short as possible, to avoid the risk
        # of having a path that's too long (a problem often seen on
        # Windows, or when using WRS tools that have their own max path
        # limitations).
        # Note that we do make sure that working_dir is an absolute
        # path, as we are likely to be changing directories when
        # running each test. A relative path would no longer work
        # under those circumstances.
        d = os.path.abspath(self.main.args.output_dir)
        self.output_dir = os.path.join(d, "new")
        self.old_output_dir = os.path.join(d, "old")

        if self.main.args.dev_temp:
            # Use a temporary directory for developers: make sure it is an
            # empty directory and disable cleanup to ease post-mortem
            # investigation.
            self.working_dir = os.path.abspath(self.main.args.dev_temp)
            rm(self.working_dir, recursive=True)
            mkdir(self.working_dir)
            self.main.args.enable_cleanup = False

        else:
            # If the temp dir is supposed to be randomized, we need to create a
            # subdirectory, so check that the parent directory exists first.
            if not os.path.isdir(self.main.args.temp_dir):
                logger.critical("temp dir '%s' does not exist",
                                self.main.args.temp_dir)
                return 1

            self.working_dir = tempfile.mkdtemp(
                "", "tmp", os.path.abspath(self.main.args.temp_dir))

        # Create the new output directory that will hold the results
        self.setup_result_dir()

        # Store in global env: target information and common paths
        self.env.output_dir = self.output_dir
        self.env.working_dir = self.working_dir
        self.env.options = self.main.args

        # User specific startup
        self.set_up()

        # Retrieve the list of test
        self.test_list = self.get_test_list(self.main.args.sublist)

        # Launch the mainloop
        self.total_test = len(self.test_list)
        self.run_test = 0

        # Status code for the result (0: success, anything else: failure)
        result = 0

        self.scheduler = Scheduler(
            job_provider=self.job_factory,
            collect=self.collect_result,
            tokens=self.main.args.jobs,
        )
        actions = DAG()
        for test in self.test_list:
            if not self.parse_test(actions, test):
                result = 1

        with open(os.path.join(self.output_dir, "tests.dot"), "w") as fd:
            fd.write(actions.as_dot())
        self.scheduler.run(actions)

        self.dump_testsuite_result()
        if self.main.args.xunit_output:
            dump_xunit_report(self, self.main.args.xunit_output)

        # Clean everything
        self.tear_down()
        return result

    def parse_test(self, actions, test_case_file):
        """Register a test.

        :param actions: the dag of actions for the testsuite
        :type actions: e3.collection.dag.DAG
        :param test_case_file: filename containing the testcase
        :type test_case_file: str

        :return: Whether the test was successfully registered.
        :rtype: bool
        """
        test_name = self.test_name(test_case_file)

        # Load testcase file
        try:
            test_env = load_with_config(
                os.path.join(self.test_dir, test_case_file), Env().to_dict()
            )
        except e3.yaml.YamlError:
            logger.error("invalid syntax for {}".format(test_case_file))
            return False

        # Ensure that the test_env act like a dictionary. We still accept None
        # as it's a shortcut for "just use default driver" configuration files.
        if test_env is None:
            test_env = {}
        elif not isinstance(test_env, collections.abc.Mapping):
            logger.error("invalid format for {}".format(test_case_file))
            return False

        # Add to the test environment the directory in which the test.yaml is
        # stored
        test_env["test_dir"] = os.path.join(
            self.env.test_dir, os.path.dirname(test_case_file)
        )
        test_env["test_case_file"] = test_case_file
        test_env["test_name"] = test_name
        test_env["working_dir"] = os.path.join(
            self.env.working_dir, test_env["test_name"]
        )

        if "driver" in test_env:
            driver = test_env["driver"]
        elif not self.default_driver:
            logger.error("missing driver for {}".format(test_case_file))
            return False
        else:
            driver = self.default_driver

        logger.debug("set driver to %s" % driver)
        if driver not in self.DRIVERS or not issubclass(
            self.DRIVERS[driver], TestDriver
        ):
            logger.error("cannot find driver for %s" % test_case_file)
            return False

        try:
            instance = self.DRIVERS[driver](self.env, test_env)
            instance.Fore = self.Fore
            instance.Style = self.Style
            instance.add_test(actions)

        except Exception as e:
            error_msg = str(e)
            error_msg += "Traceback:\n"
            error_msg += "\n".join(traceback.format_tb(sys.exc_info()[2]))
            logger.error(error_msg)
            return False

        return True

    def dump_testsuite_result(self):
        """Log a summary of test results.

        Subclasses are free to override this to do whatever is suitable for
        them.
        """
        lines = ['Summary:']

        # Display test count for each status, but only for status that have
        # at least one test. Sort them by status value, to get consistent
        # order.
        def sort_key(couple):
            status, _ = couple
            return status.value
        stats = sorted(((status, count)
                        for status, count in self.test_status_counters.items()
                        if count),
                       key=sort_key)
        for status, count in stats:
            lines.append('  {}{: <12}{} {}'.format(
                status.color(self), status.name, self.Style.RESET_ALL, count))
        if not stats:
            lines.append('  <no test result>')
        logger.info('\n'.join(lines))

        # Dump the comment file
        with open(os.path.join(self.output_dir, "comment"), "w") as f:
            self.write_comment_file(f)

    def collect_result(self, job):
        """Run internal function.

        :param job: a job that is finished
        :type job: TestFragment
        """
        self.return_values[job.uid] = job.return_value
        while job.test_instance.result_queue:
            result, tb = job.test_instance.result_queue.pop()

            # Log the test result. If requested, include test log
            log_line = '{}{: <12}{} {}{}{}'.format(
                result.status.color(self),
                result.status.name,
                self.Style.RESET_ALL,

                self.Style.BRIGHT,
                result.test_name,
                self.Style.RESET_ALL)
            if result.msg:
                log_line += ': {}{}{}'.format(self.Style.DIM, result.msg,
                                              self.Style.RESET_ALL)
            if (
                self.main.args.show_error_output and
                result.status not in (TestStatus.PASS, TestStatus.XFAIL,
                                      TestStatus.XPASS)
            ):
                log_line += '\n' + str(result.log) + self.Style.RESET_ALL
            logger.info(log_line)

            def indented_tb(tb):
                return "".join("  {}".format(line) for line in tb)

            assert result.test_name not in self.results, (
                "cannot push twice results for {}"
                "\nFirst push happened at:"
                "\n{}"
                "\nThis one happened at:"
                "\n{}".format(
                    result.test_name,
                    indented_tb(self.result_tracebacks[result.test_name]),
                    indented_tb(tb),
                )
            )
            with open(self.test_result_filename(result.test_name), "w") as fd:
                yaml.dump(result, fd)
            self.results[result.test_name] = result.status
            self.result_tracebacks[result.test_name] = tb
            self.test_counter += 1
            self.test_status_counters[result.status] += 1
        return False

    def setup_result_dir(self):
        """Create the output directory in which the results are stored."""
        if os.path.isdir(self.old_output_dir):
            rm(self.old_output_dir, True)
        if os.path.isdir(self.output_dir):
            mv(self.output_dir, self.old_output_dir)
        mkdir(self.output_dir)

        if self.main.args.dump_environ:
            with open(os.path.join(self.output_dir, "environ.sh"), "w") as f:
                for var_name in sorted(os.environ):
                    f.write("export {}={}\n".format(
                        var_name, quote_arg(os.environ[var_name])))


class Testsuite(TestsuiteCore):
    """Testsuite class.

    When implementing a new testsuite you should create a class that
    inherit from this class.
    """

    CROSS_SUPPORT = False
    # set CROSS_SUPPORT to true if the driver should accept --target, --build
    # --host switches

    TEST_SUBDIR = "."
    # Subdir in which the tests are actually stored

    DRIVERS = {}
    # Dictionary that map a name to a class that inherit from TestDriver

    @property
    def default_driver(self):
        """Return the default driver to be used.

        The return value is used only if the test.yaml file does not contain
        any ``driver`` key. Note that you have access to the current test.yaml
        location using the attribute ``self.test_case_file``.

        If the result is None, consider that "driver" is a mandatory key in
        test.yaml files.

        :return: the driver to be used by default
        :rtype: str|None
        """
        return None

    def test_name(self, test_case_file):
        """Compute the test name given a testcase spec.

        This function can be overriden. By default it uses the name of the
        directory in which the test.yaml is stored

        Note that the test name should be valid filename (not dir seprators,
        or special characters such as ``:``, ...).

        :param test_case_file: path to test.yaml file (relative to test subdir
        :type test_case_file: str | unicode
        :param variant: the test variant or None
        :type variant: str | None
        :return: the test name
        :rtype: basestring
        """
        result = (
            os.path.dirname(test_case_file)
            .replace("\\", "/")
            .rstrip("/")
            .replace("/", "__")
        )
        return result

    def get_test_list(self, sublist):
        """Retrieve the list of tests.

        The default method looks for all test.yaml files in the test
        directory. If a test.yaml has a variants field, the test is expanded
        in several test, each test being associated with a given variant.

        This function may be overriden. At this stage the self.global_env
        (after update by the set_up method) is available.

        :param sublist: a list of tests scenarios or patterns
        :type sublist: list[str]
        :return: the list of selected test
        :rtype: list[str]
        """
        # First retrive the list of test.yaml files
        result = [
            os.path.relpath(p, self.test_dir).replace("\\", "/")
            for p in find(self.test_dir, "test.yaml")
        ]
        if sublist:
            logger.info("filter: %s" % sublist)
            filtered_result = []
            path_selectors = []
            for s in sublist:
                subdir = os.path.relpath(os.path.abspath(s), self.test_dir)
                if s.endswith("/") or s.endswith("\\"):
                    subdir += "/"
                path_selectors.append(subdir)

            for p in result:
                for s in path_selectors:
                    # Either we have a match or the selected path is the tests
                    # root dir or a parent.
                    if (
                        s == "." or
                        s == "./" or
                        s.startswith("..") or
                        re.match(s, p)
                    ):
                        filtered_result.append(p)
                        continue

            result = filtered_result

        logger.info("Found %s tests", len(result))
        logger.debug("tests:\n  " + "\n  ".join(result))
        return result

    def set_up(self):
        """Execute operations before launching the testsuite.

        At this stage arguments have been read. The next step will be
        get_test_list.

        A few things can be done at this stage:

        * set some environment variables
        * adjust self.global_env (dictionary passed to all tests)
        * take into account testsuite specific options
        """
        return self.tear_up()

    def tear_up(self):
        """
        For backwards compatibility, alternative name for the "set_up" method.
        """
        pass

    def tear_down(self):
        """Execute operation when finalizing the testsuite.

        By default clean the working directory in which the tests
        were run
        """
        if self.main.args.enable_cleanup:
            rm(self.working_dir, True)

    def add_options(self):
        """Add testsuite specific switches.

        We can add your own switches by calling self.main.add_option
        function
        """
        pass

    def write_comment_file(self, comment_file):
        """Write the comment file's content.

        :param comment_file: File descriptor for the comment file.
            Overriding methods should only call its "write" method
            (or print to it).
        :type comment_file: file
        """
        pass
