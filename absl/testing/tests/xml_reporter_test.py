# Copyright 2017 The Abseil Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import io
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from xml.etree import ElementTree
from xml.parsers import expat

from absl import logging
from absl.testing import _bazelize_command
from absl.testing import absltest
from absl.testing import parameterized
from absl.testing import xml_reporter


class StringIOWriteLn(io.StringIO):

  def writeln(self, line):
    self.write(line + '\n')


class MockTest(absltest.TestCase):
  failureException = AssertionError

  def __init__(self, name):
    super().__init__()
    self.name = name

  def id(self):
    return self.name

  def runTest(self):
    return

  def shortDescription(self):
    return "This is this test's description."


# str(exception_type) is different between Python 2 and 3.
def xml_escaped_exception_type(exception_type):
  return xml_reporter._escape_xml_attr(str(exception_type))


# Matches the entire XML output. Captures all <testcase> tags except for the
# last closing </testcase> in a single group.
OUTPUT_STRING = """\
<\\?xml version="1.0"\\?>
<testsuites name="" tests="%(tests)d" failures="%(failures)d"\
 errors="%(errors)d" time="%(run_time).3f" timestamp="%(start_time)s">
<testsuite name="%(suite_name)s" tests="%(tests)d"\
 failures="%(failures)d" errors="%(errors)d" time="%(run_time).3f"\
 timestamp="%(start_time)s">
(  <testcase .*)
  </testcase>
</testsuite>
</testsuites>"""

# Matches a single <testcase> tag and its contents, without the closing
# </testcase>, which we use as a separator to split multiple <testcase> tags.
TESTCASE_STRING = """\
  <testcase name="%(test_name)s" status="%(status)s" result="%(result)s"\
 time="%(run_time).3f" classname="%(classname)s" timestamp="%(start_time)s">\
%(message)s"""

FAILURE_MESSAGE = r"""
  <failure message="e" type="{}"><!\[CDATA\[Traceback \(most recent call last\):
  File ".*xml_reporter_test\.py", line \d+, in get_sample_failure
    self.fail\(\'e\'\)(
    ~~~~~~~~~\^\^\^\^\^)?
AssertionError: e
\]\]></failure>""".format(xml_escaped_exception_type(AssertionError))

ERROR_MESSAGE = r"""
  <error message="invalid&#x20;literal&#x20;for&#x20;int\(\)&#x20;with&#x20;base&#x20;10:&#x20;(&apos;)?a(&apos;)?" type="{}"><!\[CDATA\[Traceback \(most recent call last\):
  File ".*xml_reporter_test\.py", line \d+, in get_sample_error
    int\('a'\)(
    ~~~\^\^\^\^\^)?
ValueError: invalid literal for int\(\) with base 10: '?a'?
\]\]></error>""".format(xml_escaped_exception_type(ValueError))

UNICODE_MESSAGE = r"""
  <%s message="{0}" type="{1}"><!\[CDATA\[Traceback \(most recent call last\):
  File ".*xml_reporter_test\.py", line \d+, in get_unicode_sample_failure
    raise AssertionError\('\\xe9'\)
AssertionError: {0}
\]\]></%s>""".format(r'\xe9', xml_escaped_exception_type(AssertionError))

NEWLINE_MESSAGE = r"""
  <%s message="{}" type="{}"><!\[CDATA\[Traceback \(most recent call last\):
  File ".*xml_reporter_test\.py", line \d+, in get_newline_message_sample_failure
    raise AssertionError\(\'{}'\)
AssertionError: {}
\]\]></%s>""".format(
    'new&#xA;line',
    xml_escaped_exception_type(AssertionError),
    r'new\\nline',
    'new\nline',
)

UNEXPECTED_SUCCESS_MESSAGE = '\n'.join([
    '',
    (r'  <error message="" type=""><!\[CDATA\[Test case '
     r'__main__.MockTest.unexpectedly_passing_test should have failed, '
     r'but passed.\]\]></error>'),
])

UNICODE_ERROR_MESSAGE = UNICODE_MESSAGE % ('error', 'error')
NEWLINE_ERROR_MESSAGE = NEWLINE_MESSAGE % ('error', 'error')


class TextAndXMLTestResultTest(absltest.TestCase):

  def setUp(self):
    super().setUp()
    self.stream = StringIOWriteLn()
    self.xml_stream = io.StringIO()

  def _make_result(self, times):
    timer = mock.Mock()
    timer.side_effect = times
    return xml_reporter._TextAndXMLTestResult(self.xml_stream, self.stream,
                                              'foo', 0, timer)

  def _assert_match(self, regex, output, flags=0):
    fail_msg = f'Expected regex:\n{regex}\nTo match:\n{output}'
    result = re.match(regex, output, flags)
    if result is None:
      self.fail(fail_msg)
    return result.groups()

  def _assert_valid_xml(self, xml_output):
    try:
      expat.ParserCreate().Parse(xml_output)
    except expat.ExpatError as e:
      raise AssertionError(f'Bad XML output: {e}\n{xml_output}')

  def _simulate_error_test(self, test, result):
    result.startTest(test)
    result.addError(test, self.get_sample_error())
    result.stopTest(test)

  def _simulate_failing_test(self, test, result):
    result.startTest(test)
    result.addFailure(test, self.get_sample_failure())
    result.stopTest(test)

  def _simulate_passing_test(self, test, result):
    result.startTest(test)
    result.addSuccess(test)
    result.stopTest(test)

  def _iso_timestamp(self, timestamp):
    return datetime.datetime.utcfromtimestamp(timestamp).isoformat() + '+00:00'

  def test_with_passing_test(self):
    start_time = 0
    end_time = 2
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.passing_test')
    result.startTestRun()
    result.startTest(test)
    result.addSuccess(test)
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 0,
        'errors': 0,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': 'passing_test',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        'message': ''
    }
    (testcase,) = self._assert_match(expected_re, self.xml_stream.getvalue(),
                                     re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def test_with_passing_subtest(self):
    start_time = 0
    end_time = 2
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.passing_test')
    subtest = unittest.case._SubTest(test, 'msg', None)  # pytype: disable=module-attr
    result.startTestRun()
    result.startTest(test)
    result.addSubTest(test, subtest, None)
    result.stopTestRun()
    result.printErrors()

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 0,
        'errors': 0,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': r'passing_test&#x20;\[msg\]',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        'message': ''
    }
    (testcase,) = self._assert_match(expected_re, self.xml_stream.getvalue(),
                                     re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def test_with_passing_subtest_with_dots_in_parameter_name(self):
    start_time = 0
    end_time = 2
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.passing_test')
    subtest = unittest.case._SubTest(test, 'msg', {'case': 'a.b.c'})  # pytype: disable=module-attr
    result.startTestRun()
    result.startTest(test)
    result.addSubTest(test, subtest, None)
    result.stopTestRun()
    result.printErrors()

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 0,
        'errors': 0,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name':
            r'passing_test&#x20;\[msg\]&#x20;\(case=&apos;a.b.c&apos;\)',
        'classname':
            '__main__.MockTest',
        'status':
            'run',
        'result':
            'completed',
        'attributes':
            '',
        'message':
            ''
    }
    (testcase,) = self._assert_match(expected_re, self.xml_stream.getvalue(),
                                     re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def get_sample_error(self):
    try:
      int('a')
    except ValueError:
      error_values = sys.exc_info()
      return error_values

  def get_sample_failure(self):
    try:
      self.fail('e')
    except AssertionError:
      error_values = sys.exc_info()
      return error_values

  def get_newline_message_sample_failure(self):
    try:
      raise AssertionError('new\nline')
    except AssertionError:
      error_values = sys.exc_info()
      return error_values

  def get_unicode_sample_failure(self):
    try:
      raise AssertionError('\xe9')
    except AssertionError:
      error_values = sys.exc_info()
      return error_values

  def get_terminal_escape_sample_failure(self):
    try:
      raise AssertionError('\x1b')
    except AssertionError:
      error_values = sys.exc_info()
      return error_values

  def test_with_failing_test(self):
    start_time = 10
    end_time = 20
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.failing_test')
    result.startTestRun()
    result.startTest(test)
    result.addFailure(test, self.get_sample_failure())
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 1,
        'errors': 0,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': 'failing_test',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        'message': FAILURE_MESSAGE
    }
    (testcase,) = self._assert_match(expected_re, self.xml_stream.getvalue(),
                                     re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def test_with_failing_subtest(self):
    start_time = 10
    end_time = 20
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.failing_test')
    subtest = unittest.case._SubTest(test, 'msg', None)  # pytype: disable=module-attr
    result.startTestRun()
    result.startTest(test)
    result.addSubTest(test, subtest, self.get_sample_failure())
    result.stopTestRun()
    result.printErrors()

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 1,
        'errors': 0,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': r'failing_test&#x20;\[msg\]',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        'message': FAILURE_MESSAGE
    }
    (testcase,) = self._assert_match(expected_re, self.xml_stream.getvalue(),
                                     re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def test_with_error_test(self):
    start_time = 100
    end_time = 200
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.failing_test')
    result.startTestRun()
    result.startTest(test)
    result.addError(test, self.get_sample_error())
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()
    xml = self.xml_stream.getvalue()

    self._assert_valid_xml(xml)

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 0,
        'errors': 1,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': 'failing_test',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        'message': ERROR_MESSAGE
    }
    (testcase,) = self._assert_match(expected_re, xml, re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def test_with_error_subtest(self):
    start_time = 10
    end_time = 20
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.error_test')
    subtest = unittest.case._SubTest(test, 'msg', None)  # pytype: disable=module-attr
    result.startTestRun()
    result.startTest(test)
    result.addSubTest(test, subtest, self.get_sample_error())
    result.stopTestRun()
    result.printErrors()

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 0,
        'errors': 1,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': r'error_test&#x20;\[msg\]',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        'message': ERROR_MESSAGE
    }
    (testcase,) = self._assert_match(expected_re, self.xml_stream.getvalue(),
                                     re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def test_with_fail_and_error_test(self):
    """Tests a failure and subsequent error within a single result."""
    start_time = 123
    end_time = 456
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.failing_test')
    result.startTestRun()
    result.startTest(test)
    result.addFailure(test, self.get_sample_failure())
    # This could happen in tearDown
    result.addError(test, self.get_sample_error())
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()
    xml = self.xml_stream.getvalue()

    self._assert_valid_xml(xml)

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 1,  # Only the failure is tallied (because it was first).
        'errors': 0,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': 'failing_test',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        # Messages from failure and error should be concatenated in order.
        'message': FAILURE_MESSAGE + ERROR_MESSAGE
    }
    (testcase,) = self._assert_match(expected_re, xml, re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def test_with_error_and_fail_test(self):
    """Tests an error and subsequent failure within a single result."""
    start_time = 123
    end_time = 456
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.failing_test')
    result.startTestRun()
    result.startTest(test)
    result.addError(test, self.get_sample_error())
    result.addFailure(test, self.get_sample_failure())
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()
    xml = self.xml_stream.getvalue()

    self._assert_valid_xml(xml)

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 0,
        'errors': 1,  # Only the error is tallied (because it was first).
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': 'failing_test',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        # Messages from error and failure should be concatenated in order.
        'message': ERROR_MESSAGE + FAILURE_MESSAGE
    }
    (testcase,) = self._assert_match(expected_re, xml, re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def test_with_newline_error_test(self):
    start_time = 100
    end_time = 200
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.failing_test')
    result.startTestRun()
    result.startTest(test)
    result.addError(test, self.get_newline_message_sample_failure())
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()
    xml = self.xml_stream.getvalue()

    self._assert_valid_xml(xml)

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 0,
        'errors': 1,
        'run_time': run_time,
        'start_time': start_time_str,
    } + '\n'
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': 'failing_test',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        'message': NEWLINE_ERROR_MESSAGE
    }
    (testcase,) = self._assert_match(expected_re, xml, re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def test_with_unicode_error_test(self):
    start_time = 100
    end_time = 200
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.failing_test')
    result.startTestRun()
    result.startTest(test)
    result.addError(test, self.get_unicode_sample_failure())
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()
    xml = self.xml_stream.getvalue()

    self._assert_valid_xml(xml)

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 0,
        'errors': 1,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': 'failing_test',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        'message': UNICODE_ERROR_MESSAGE
    }
    (testcase,) = self._assert_match(expected_re, xml, re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def test_with_terminal_escape_error(self):
    start_time = 100
    end_time = 200
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.failing_test')
    result.startTestRun()
    result.startTest(test)
    result.addError(test, self.get_terminal_escape_sample_failure())
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()

    self._assert_valid_xml(self.xml_stream.getvalue())

  def test_with_expected_failure_test(self):
    start_time = 100
    end_time = 200
    result = self._make_result((start_time, start_time, end_time, end_time))
    error_values = ''

    try:
      raise RuntimeError('Test expectedFailure')
    except RuntimeError:
      error_values = sys.exc_info()

    test = MockTest('__main__.MockTest.expected_failing_test')
    result.startTestRun()
    result.startTest(test)
    result.addExpectedFailure(test, error_values)
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 0,
        'errors': 0,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': 'expected_failing_test',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        'message': ''
    }
    (testcase,) = self._assert_match(expected_re, self.xml_stream.getvalue(),
                                     re.DOTALL)
    self._assert_match(expected_testcase_re, testcase, re.DOTALL)

  def test_with_unexpected_success_error_test(self):
    start_time = 100
    end_time = 200
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.unexpectedly_passing_test')
    result.startTestRun()
    result.startTest(test)
    result.addUnexpectedSuccess(test)
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 0,
        'errors': 1,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': 'unexpectedly_passing_test',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        'message': UNEXPECTED_SUCCESS_MESSAGE
    }
    (testcase,) = self._assert_match(expected_re, self.xml_stream.getvalue(),
                                     re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def test_with_skipped_test(self):
    start_time = 100
    end_time = 100
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.skipped_test_with_reason')
    result.startTestRun()
    result.startTest(test)
    result.addSkip(test, 'b"r')
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 0,
        'errors': 0,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': 'skipped_test_with_reason',
        'classname': '__main__.MockTest',
        'status': 'notrun',
        'result': 'suppressed',
        'message': ''
    }
    (testcase,) = self._assert_match(expected_re, self.xml_stream.getvalue(),
                                     re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def test_two_tests_with_time(self):
    start_time1 = 100
    end_time1 = 200
    start_time2 = 400
    end_time2 = 700
    name = '__main__.MockTest.'
    result = self._make_result((start_time1, start_time1, end_time1,
                                start_time2, end_time2, end_time2))

    test = MockTest(f'{name}one_test')
    result.startTestRun()
    result.startTest(test)
    result.addSuccess(test)
    result.stopTest(test)

    test = MockTest(f'{name}another_test')
    result.startTest(test)
    result.addSuccess(test)
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()

    start_time = min(start_time1, start_time2)
    run_time = max(end_time1, end_time2) - start_time
    start_time_str = re.escape(self._iso_timestamp(start_time))
    start_time_str1 = re.escape(self._iso_timestamp(start_time1))
    start_time_str2 = re.escape(self._iso_timestamp(start_time2))
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 2,
        'failures': 0,
        'errors': 0,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase1_re = TESTCASE_STRING % {
        'run_time': end_time1 - start_time1,
        'start_time': start_time_str1,
        'test_name': 'one_test',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'message': ''
    }
    expected_testcase2_re = TESTCASE_STRING % {
        'run_time': end_time2 - start_time2,
        'start_time': start_time_str2,
        'test_name': 'another_test',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'message': ''
    }

    (testcases,) = self._assert_match(expected_re, self.xml_stream.getvalue(),
                                      re.DOTALL)
    [testcase1, testcase2] = testcases.split('\n  </testcase>\n')
    # Sorting by test name flips the order of the two tests.
    self._assert_match(expected_testcase2_re, testcase1)
    self._assert_match(expected_testcase1_re, testcase2)

  def test_with_no_suite_name(self):
    start_time = 1000
    end_time = 1200
    result = self._make_result((start_time, start_time, end_time, end_time))

    test = MockTest('__main__.MockTest.bad_name')
    result.startTestRun()
    result.startTest(test)
    result.addSuccess(test)
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    expected_re = OUTPUT_STRING % {
        'suite_name': 'MockTest',
        'tests': 1,
        'failures': 0,
        'errors': 0,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': 'bad_name',
        'classname': '__main__.MockTest',
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        'message': ''
    }
    (testcase,) = self._assert_match(expected_re, self.xml_stream.getvalue(),
                                     re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def test_unnamed_parameterized_testcase(self):
    """Test unnamed parameterized test cases.

    Unnamed parameterized test cases might have non-alphanumeric characters in
    their test method names. This test ensures xml_reporter handles them
    correctly.
    """

    class ParameterizedTest(parameterized.TestCase):

      @parameterized.parameters(('a (b.c)',))
      def test_prefix(self, case):
        self.assertTrue(case.startswith('a'))

    start_time = 1000
    end_time = 1200
    result = self._make_result((start_time, start_time, end_time, end_time))
    test = ParameterizedTest(methodName='test_prefix0')
    result.startTestRun()
    result.startTest(test)
    result.addSuccess(test)
    result.stopTest(test)
    result.stopTestRun()
    result.printErrors()

    start_time_str = re.escape(self._iso_timestamp(start_time))
    run_time = end_time - start_time
    classname = xml_reporter._escape_xml_attr(
        unittest.util.strclass(test.__class__))
    expected_re = OUTPUT_STRING % {
        'suite_name': 'ParameterizedTest',
        'tests': 1,
        'failures': 0,
        'errors': 0,
        'run_time': run_time,
        'start_time': start_time_str,
    }
    expected_testcase_re = TESTCASE_STRING % {
        'run_time': run_time,
        'start_time': start_time_str,
        'test_name': re.escape('test_prefix0&#x20;(&apos;a&#x20;(b.c)&apos;)'),
        'classname': classname,
        'status': 'run',
        'result': 'completed',
        'attributes': '',
        'message': ''
    }
    (testcase,) = self._assert_match(expected_re, self.xml_stream.getvalue(),
                                     re.DOTALL)
    self._assert_match(expected_testcase_re, testcase)

  def teststop_test_without_pending_test(self):
    end_time = 1200
    result = self._make_result((end_time,))

    test = MockTest('__main__.MockTest.bad_name')
    result.stopTest(test)
    result.stopTestRun()
    # Just verify that this doesn't crash

  def test_text_and_xmltest_runner(self):
    runner = xml_reporter.TextAndXMLTestRunner(self.xml_stream, self.stream,
                                               'foo', 1)
    result1 = runner._makeResult()
    result2 = xml_reporter._TextAndXMLTestResult(None, None, None, 0, None)
    self.assertIs(type(result1), type(result2))

  def test_timing_with_time_stub(self):
    """Make sure that timing is correct even if time.time is stubbed out."""
    saved_time = time.time
    try:
      time.time = lambda: -1
      reporter = xml_reporter._TextAndXMLTestResult(self.xml_stream,
                                                    self.stream,
                                                    'foo', 0)
      test = MockTest('bar')
      reporter.startTest(test)
      self.assertNotEqual(reporter.start_time, -1)
    finally:
      time.time = saved_time

  def test_concurrent_add_and_delete_pending_test_case_result(self):
    """Make sure adding/deleting pending test case results are thread safe."""
    result = xml_reporter._TextAndXMLTestResult(None, self.stream, None, 0,
                                                None)
    def add_and_delete_pending_test_case_result(test_name):
      test = MockTest(test_name)
      result.addSuccess(test)
      result.delete_pending_test_case_result(test)

    for i in range(50):
      add_and_delete_pending_test_case_result('add_and_delete_test%s' % i)
    self.assertEqual(result.pending_test_case_results, {})

  def test_concurrent_test_runs(self):
    """Make sure concurrent test runs do not race each other."""
    num_passing_tests = 20
    num_failing_tests = 20
    num_error_tests = 20
    total_num_tests = num_passing_tests + num_failing_tests + num_error_tests

    times = [0] + [i for i in range(2 * total_num_tests)
                  ] + [2 * total_num_tests - 1]
    result = self._make_result(times)
    threads = []
    names = []
    result.startTestRun()
    for i in range(num_passing_tests):
      name = 'passing_concurrent_test_%s' % i
      names.append(name)
      test_name = '__main__.MockTest.%s' % name
      # xml_reporter uses id(test) as the test identifier.
      # In a real testing scenario, all the test instances are created before
      # running them. So all ids will be unique.
      # We must do the same here: create test instance beforehand.
      test = MockTest(test_name)
      threads.append(threading.Thread(
          target=self._simulate_passing_test, args=(test, result)))
    for i in range(num_failing_tests):
      name = 'failing_concurrent_test_%s' % i
      names.append(name)
      test_name = '__main__.MockTest.%s' % name
      test = MockTest(test_name)
      threads.append(threading.Thread(
          target=self._simulate_failing_test, args=(test, result)))
    for i in range(num_error_tests):
      name = 'error_concurrent_test_%s' % i
      names.append(name)
      test_name = '__main__.MockTest.%s' % name
      test = MockTest(test_name)
      threads.append(threading.Thread(
          target=self._simulate_error_test, args=(test, result)))
    for t in threads:
      t.start()
    for t in threads:
      t.join()

    result.stopTestRun()
    result.printErrors()
    tests_not_in_xml = []
    for tn in names:
      if tn not in self.xml_stream.getvalue():
        tests_not_in_xml.append(tn)
    msg = ('Expected xml_stream to contain all test %s results, but %s tests '
           'are missing. List of missing tests: %s' % (
               total_num_tests, len(tests_not_in_xml), tests_not_in_xml))
    self.assertEqual([], tests_not_in_xml, msg)

  def test_add_failure_during_stop_test(self):
    """Tests an addFailure() call from within a stopTest() call stack."""
    result = self._make_result((0, 2))
    test = MockTest('__main__.MockTest.failing_test')
    result.startTestRun()
    result.startTest(test)

    # Replace parent stopTest method from unittest.TextTestResult with
    # a version that calls self.addFailure().
    with mock.patch.object(
        unittest.TextTestResult,
        'stopTest',
        side_effect=lambda t: result.addFailure(t, self.get_sample_failure())):
      # Run stopTest in a separate thread since we are looking to verify that
      # it does not deadlock, and would otherwise prevent the test from
      # completing.
      stop_test_thread = threading.Thread(target=result.stopTest, args=(test,))
      stop_test_thread.daemon = True
      stop_test_thread.start()

    stop_test_thread.join(10.0)
    self.assertFalse(stop_test_thread.is_alive(),
                     'result.stopTest(test) call failed to complete')


class XMLTest(absltest.TestCase):

  def test_escape_xml(self):
    self.assertEqual(xml_reporter._escape_xml_attr('"Hi" <\'>\t\r\n'),
                     '&quot;Hi&quot;&#x20;&lt;&apos;&gt;&#x9;&#xD;&#xA;')


class XmlReporterFixtureTest(absltest.TestCase):

  def _get_helper(self):
    binary_name = 'absl/testing/tests/xml_reporter_helper_test'
    return _bazelize_command.get_executable_path(binary_name)

  def _run_test_and_get_xml(self, flag):
    """Runs xml_reporter_helper_test and returns an Element instance.

    Runs xml_reporter_helper_test in a new process so that it can
    exercise the entire test infrastructure, and easily test issues in
    the test fixture.

    Args:
      flag: flag to pass to xml_reporter_helper_test

    Returns:
      The Element instance of the XML output.
    """

    xml_fhandle, xml_fname = tempfile.mkstemp()
    os.close(xml_fhandle)

    try:
      binary = self._get_helper()
      args = [binary, flag, '--xml_output_file=%s' % xml_fname]
      ret = subprocess.call(args)
      self.assertEqual(ret, 0)

      xml = ElementTree.parse(xml_fname).getroot()
    finally:
      os.remove(xml_fname)

    return xml

  def _run_test(self, flag, num_errors, num_failures, suites):
    xml_fhandle, xml_fname = tempfile.mkstemp()
    os.close(xml_fhandle)

    try:
      binary = self._get_helper()
      args = [binary, flag, '--xml_output_file=%s' % xml_fname]
      ret = subprocess.call(args)
      self.assertNotEqual(ret, 0)

      xml = ElementTree.parse(xml_fname).getroot()
      logging.info('xml output is:\n%s', ElementTree.tostring(xml))
    finally:
      os.remove(xml_fname)

    self.assertEqual(int(xml.attrib['errors']), num_errors)
    self.assertEqual(int(xml.attrib['failures']), num_failures)
    self.assertLen(xml, len(suites))
    actual_suites = sorted(
        xml.findall('testsuite'), key=lambda x: x.attrib['name'])
    suites = sorted(suites, key=lambda x: x['name'])
    for actual_suite, expected_suite in zip(actual_suites, suites):
      self.assertEqual(actual_suite.attrib['name'], expected_suite['name'])
      self.assertLen(actual_suite, len(expected_suite['cases']))
      actual_cases = sorted(actual_suite.findall('testcase'),
                            key=lambda x: x.attrib['name'])
      expected_cases = sorted(expected_suite['cases'], key=lambda x: x['name'])
      for actual_case, expected_case in zip(actual_cases, expected_cases):
        self.assertEqual(actual_case.attrib['name'], expected_case['name'])
        self.assertEqual(actual_case.attrib['classname'],
                         expected_case['classname'])
        if 'error' in expected_case:
          actual_error = actual_case.find('error')
          self.assertEqual(actual_error.attrib['message'],
                           expected_case['error'])
        if 'failure' in expected_case:
          actual_failure = actual_case.find('failure')
          self.assertEqual(actual_failure.attrib['message'],
                           expected_case['failure'])

    return xml

  def test_set_up_module_error(self):
    self._run_test(
        flag='--set_up_module_error',
        num_errors=1,
        num_failures=0,
        suites=[{'name': '__main__',
                 'cases': [{'name': 'setUpModule',
                            'classname': '__main__',
                            'error': 'setUpModule Errored!'}]}])

  def test_tear_down_module_error(self):
    self._run_test(
        flag='--tear_down_module_error',
        num_errors=1,
        num_failures=0,
        suites=[{'name': 'FailableTest',
                 'cases': [{'name': 'test',
                            'classname': '__main__.FailableTest'}]},
                {'name': '__main__',
                 'cases': [{'name': 'tearDownModule',
                            'classname': '__main__',
                            'error': 'tearDownModule Errored!'}]}])

  def test_set_up_class_error(self):
    self._run_test(
        flag='--set_up_class_error',
        num_errors=1,
        num_failures=0,
        suites=[{'name': 'FailableTest',
                 'cases': [{'name': 'setUpClass',
                            'classname': '__main__.FailableTest',
                            'error': 'setUpClass Errored!'}]}])

  def test_tear_down_class_error(self):
    self._run_test(
        flag='--tear_down_class_error',
        num_errors=1,
        num_failures=0,
        suites=[{'name': 'FailableTest',
                 'cases': [{'name': 'test',
                            'classname': '__main__.FailableTest'},
                           {'name': 'tearDownClass',
                            'classname': '__main__.FailableTest',
                            'error': 'tearDownClass Errored!'}]}])

  def test_set_up_error(self):
    self._run_test(
        flag='--set_up_error',
        num_errors=1,
        num_failures=0,
        suites=[{'name': 'FailableTest',
                 'cases': [{'name': 'test',
                            'classname': '__main__.FailableTest',
                            'error': 'setUp Errored!'}]}])

  def test_tear_down_error(self):
    self._run_test(
        flag='--tear_down_error',
        num_errors=1,
        num_failures=0,
        suites=[{'name': 'FailableTest',
                 'cases': [{'name': 'test',
                            'classname': '__main__.FailableTest',
                            'error': 'tearDown Errored!'}]}])

  def test_test_error(self):
    self._run_test(
        flag='--test_error',
        num_errors=1,
        num_failures=0,
        suites=[{'name': 'FailableTest',
                 'cases': [{'name': 'test',
                            'classname': '__main__.FailableTest',
                            'error': 'test Errored!'}]}])

  def test_set_up_failure(self):
    self._run_test(
        flag='--set_up_fail',
        num_errors=0,
        num_failures=1,
        suites=[{'name': 'FailableTest',
                 'cases': [{'name': 'test',
                            'classname': '__main__.FailableTest',
                            'failure': 'setUp Failed!'}]}])

  def test_tear_down_failure(self):
    self._run_test(
        flag='--tear_down_fail',
        num_errors=0,
        num_failures=1,
        suites=[{'name': 'FailableTest',
                 'cases': [{'name': 'test',
                            'classname': '__main__.FailableTest',
                            'failure': 'tearDown Failed!'}]}])

  def test_test_fail(self):
    self._run_test(
        flag='--test_fail',
        num_errors=0,
        num_failures=1,
        suites=[{'name': 'FailableTest',
                 'cases': [{'name': 'test',
                            'classname': '__main__.FailableTest',
                            'failure': 'test Failed!'}]}])

  def test_test_randomization_seed_logging(self):
    # We expect the resulting XML to start as follows:
    # <testsuites ...>
    #  <properties>
    #   <property name="test_randomize_ordering_seed" value="17" />
    # ...
    #
    # which we validate here.
    out = self._run_test_and_get_xml('--test_randomize_ordering_seed=17')
    expected_attrib = {'name': 'test_randomize_ordering_seed', 'value': '17'}
    property_attributes = [
        prop.attrib for prop in out.findall('./properties/property')]
    self.assertIn(expected_attrib, property_attributes)


if __name__ == '__main__':
  absltest.main()
