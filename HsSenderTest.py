#!/usr/bin/env python

import json
import logging
import numbers
import os
import re
import shutil
import tarfile
import tempfile
import time
import traceback
import unittest

import DAQTime
import HsConstants
import HsSender
import HsMessage
import HsUtil

from HsBase import HsBase
from HsException import HsException
from HsTestUtil import Mock0MQPoller, Mock0MQSocket, MockHitspool, \
    MockI3Socket, TIME_PAT, set_state_db_path
from LoggingTestCase import LoggingTestCase
from RequestMonitor import RequestMonitor


class MySender(HsSender.HsSender):
    """
    Use mock 0MQ sockets for testing
    """
    def __init__(self, verbose=False):
        self.__i3_sock = None
        self.__poll_sock = None
        self.__rptr_sock = None

        super(MySender, self).__init__(host="tstsnd", is_test=True)

        # unit tests were running to completion before the RequestMonitor had
        # started, causing tearDown() to hang on `monitor.join()`
        while not self.monitor_started:
            time.sleep(0.1)

        if verbose:
            self.__i3_sock.set_verbose()
            self.__rptr_sock.set_verbose()

    def create_i3socket(self, host):
        if self.__i3_sock is not None:
            raise Exception("Cannot create multiple I3 sockets")

        self.__i3_sock = MockI3Socket('HsPublisher')
        return self.__i3_sock

    def create_poller(self):
        if self.__poll_sock is not None:
            raise Exception("Cannot create multiple I3 sockets")

        self.__poll_sock = Mock0MQPoller("Poller")
        return self.__poll_sock

    def create_reporter(self):
        if self.__rptr_sock is not None:
            raise Exception("Cannot create multiple Reporter sockets")

        self.__rptr_sock = Mock0MQSocket("Reporter")
        return self.__rptr_sock

    def validate(self):
        """
        Check that all expected messages were received by mock sockets
        """
        for sock in (self.__rptr_sock, self.__poll_sock, self.__i3_sock):
            if sock is not None:
                sock.validate()


class FailableSender(MySender):
    """
    Override crucial methods to test failure modes
    """
    def __init__(self):
        super(FailableSender, self).__init__()

        self.__fail_move_file = False
        self.__fail_touch_file = False
        self.__fail_tar_file = False
        self.__moved_files = False

    def fail_create_sem_file(self):
        self.__fail_touch_file = True

    def fail_create_tar_file(self):
        self.__fail_tar_file = True

    def fail_move_file(self):
        self.__fail_move_file = True

    def move_file(self, src, dst):
        if self.__fail_move_file:
            raise HsException("Fake Move Error")

    def moved_files(self):
        return self.__moved_files

    def movefiles(self, copydir, targetdir):
        self.__moved_files = True
        return True

    def remove_tree(self, path):
        pass

    def write_meta_xml(self, spadedir, basename, start_ticks, stop_ticks):
        if self.__fail_touch_file:
            raise HsException("Fake Touch Error")
        return basename + HsSender.HsSender.META_SUFFIX

    def write_sem(self, spadedir, basename):
        if self.__fail_touch_file:
            raise HsException("Fake Touch Error")
        return basename + HsSender.HsSender.SEM_SUFFIX

    def write_tarfile(self, sourcedir, sourcefiles, tarname):
        if self.__fail_tar_file > 0:
            self.__fail_tar_file -= 1
            raise HsException("Fake Tar Error")


class TestException(Exception):
    pass


class MockRequestBuilder(object):
    SNDAQ = 1
    HESE = 2
    ANON = 3

    USRDIR = None

    def __init__(self, req_id, req_type, username, start_ticks, stop_ticks,
                 timetag, host, firstfile, numfiles):
        if start_ticks is not None and \
           not isinstance(start_ticks, numbers.Number):
            raise TypeError("Start time %s<%s> is not number" %
                            (start_ticks, type(start_ticks).__name__))
        if stop_ticks is not None and \
           not isinstance(stop_ticks, numbers.Number):
            raise TypeError("Stop time %s<%s> is not number" %
                            (stop_ticks, type(stop_ticks).__name__))

        self.__req_id = req_id
        self.__start_ticks = start_ticks
        self.__stop_ticks = stop_ticks
        self.__username = username
        self.__host = host
        self.__firstfile = firstfile
        self.__numfiles = numfiles

        # create initial directory
        category = self.__get_category(req_type)
        self.__hsdir = MockHitspool.create_copy_files(category, timetag, host,
                                                      firstfile, numfiles,
                                                      real_stuff=True)

        # build final directory path
        if self.USRDIR is None:
            self.create_user_dir()
        self.__destdir = os.path.join(self.USRDIR,
                                      "%s_%s_%s" % (category, timetag, host))

    def __get_category(self, reqtype):
        if reqtype == self.SNDAQ:
            return "SNALERT"
        if reqtype == self.HESE:
            return "HESE"
        if reqtype == self.ANON:
            return "ANON"
        if isinstance(reqtype, (str, unicode)):
            return reqtype
        raise NotImplementedError("Unknown request type #%s" % reqtype)

    def add_i3live_message(self, i3socket, status, success=None, failed=None):
        start_utc = DAQTime.ticks_to_utc(self.__start_ticks)
        stop_utc = DAQTime.ticks_to_utc(self.__stop_ticks)

        # build I3Live success message
        value = {
            'status': status,
            'request_id': self.__req_id,
            'username': self.__username,
            'start_time': start_utc.strftime(DAQTime.TIME_FORMAT),
            'stop_time': stop_utc.strftime(DAQTime.TIME_FORMAT),
            'destination_dir': self.__destdir,
            'prefix': None,
            'update_time': TIME_PAT,
        }

        if success is not None:
            value["success"] = success
        if failed is not None:
            value["success"] = failed

        # add all expected I3Live messages
        i3socket.add_expected_message(value, service="hitspool",
                                      varname="hsrequest_info", time=TIME_PAT,
                                      prio=1)

    @classmethod
    def add_reporter_request(cls, reporter, msgtype, req_id, username,
                             start_ticks, stop_ticks, host, hsdir, destdir,
                             success=None, failed=None):
        # initialize message
        rcv_msg = {
            "msgtype": msgtype,
            "request_id": req_id,
            "username": username,
            "start_ticks": start_ticks,
            "stop_ticks": stop_ticks,
            "copy_dir": hsdir,
            "destination_dir": destdir,
            "prefix": None,
            "extract": None,
            "host": host,
            "version": HsMessage.CURRENT_VERSION,
        }

        if success is not None:
            rcv_msg["success"] = success
        if failed is not None:
            rcv_msg["failed"] = failed

        # add all expected JSON messages
        reporter.add_incoming(rcv_msg)

    def add_request(self, reporter, msgtype, success=None, failed=None):
        self.add_reporter_request(reporter, msgtype, self.__req_id,
                                  self.__username, self.__start_ticks,
                                  self.__stop_ticks, self.__host,
                                  self.__hsdir, self.__destdir,
                                  success=success, failed=failed)

    def check_files(self):
        if os.path.exists(self.__hsdir):
            raise TestException("HitSpool directory \"%s\" was not moved" %
                                self.__hsdir)
        if not os.path.exists(self.USRDIR):
            raise TestException("User directory \"%s\" does not exist" %
                                self.USRDIR)

        base = os.path.basename(self.__hsdir)
        if base == "":
            base = os.path.basename(os.path.dirname(self.__hsdir))
            if base == "":
                raise TestException("Cannot find basename from %s" %
                                    self.__hsdir)

        if os.path.basename(self.__destdir) == base:
            subdir = self.__destdir
        else:
            subdir = os.path.join(self.__destdir, base)

        if not os.path.exists(subdir):
            raise TestException("Moved directory \"%s\" does not exist" %
                                subdir)

        flist = []
        for entry in os.listdir(subdir):
            flist.append(entry)

        self.check_hitspool_file_list(flist, self.__firstfile, self.__numfiles)

    @classmethod
    def check_hitspool_file_list(cls, flist, firstfile, numfiles):
        flist.sort()

        for fnum in range(firstfile, firstfile + numfiles):
            fname = "HitSpool-%d" % fnum
            if len(flist) == 0:
                raise TestException("Not all files were copied"
                                    " (found %d of %d)" %
                                    (fnum - firstfile, numfiles))

            if fname != flist[0]:
                raise TestException("Expected file #%d to be \"%s\""
                                    " not \"%s\"" %
                                    (fnum - firstfile, fname, flist[0]))

            del flist[0]

        if len(flist) != 0:
            raise TestException("%d extra files were copied (%s)" %
                                (len(flist), flist))

    @classmethod
    def create_user_dir(cls):
        usrdir = cls.get_user_dir()
        if os.path.exists(usrdir):
            raise TestException("UserDir %s already exists" % str(usrdir))
        os.makedirs(usrdir)
        return usrdir

    @property
    def destdir(self):
        return self.__destdir

    @classmethod
    def destroy(cls):
        if cls.USRDIR is not None:
            # clear lingering files
            try:
                shutil.rmtree(cls.USRDIR)
            except:
                pass
            cls.USRDIR = None

    @classmethod
    def get_user_dir(cls):
        if cls.USRDIR is None:
            if MockHitspool.COPY_DIR is not None:
                cls.USRDIR = os.path.join(MockHitspool.COPY_DIR, "UserCopy")
        return cls.USRDIR

    @property
    def host(self):
        return self.__host

    @property
    def hsdir(self):
        return self.__hsdir

    @property
    def req_id(self):
        return self.__req_id


class HsSenderTest(LoggingTestCase):
    # pylint: disable=too-many-public-methods
    # Really?!?!  In a test class?!?!  Shut up, pylint!

    SENDER = None
    CACHED_COPY_PATH = None

    def __check_hitspool_file_list(self, flist, firstnum, numfiles):
        flist.sort()

        for fnum in range(firstnum, firstnum + numfiles):
            fname = "HitSpool-%d" % fnum
            if len(flist) == 0:
                self.fail("Not all files were copied (found %d of %d)" %
                          (fnum - firstnum, numfiles))

            self.assertEqual(fname, flist[0],
                             "Expected file #%d to be \"%s\" not \"%s\"" %
                             (fnum - firstnum, fname, flist[0]))
            del flist[0]
        if len(flist) != 0:
            self.fail("%d extra files were copied (%s)" % (len(flist), flist))

    @classmethod
    def close_all_senders(cls):
        found_error = False
        if cls.SENDER is not None:
            if not cls.SENDER.has_monitor:
                logging.error("Sender monitor has died")
                found_error = True
            try:
                cls.SENDER.close_all()
            except:
                traceback.print_exc()
                found_error = True
            cls.SENDER = None
        return found_error

    @classmethod
    def set_sender(cls, sndr):
        cls.SENDER = sndr

    def setUp(self):
        super(HsSenderTest, self).setUp()
        # by default, check all log messages
        self.setLogLevel(0)

        # point the RequestMonitor at a temporary state file for tests
        set_state_db_path()

        # get rid of HsSender's state database
        dbpath = RequestMonitor.get_db_path()
        if os.path.exists(dbpath):
            os.unlink(dbpath)

        self.set_copy_path()

    def tearDown(self):
        try:
            super(HsSenderTest, self).tearDown()
        finally:
            found_error = False

            self.restore_copy_path()

            # clear lingering files
            try:
                MockHitspool.destroy()
            except:
                traceback.print_exc()
                found_error = True

            try:
                MockRequestBuilder.destroy()
            except:
                traceback.print_exc()
                found_error = True

            # close all sockets

            found_error |= self.close_all_senders()

            # get rid of HsSender's state database
            dbpath = RequestMonitor.get_db_path()
            if os.path.exists(dbpath):
                try:
                    os.unlink(dbpath)
                except:
                    traceback.print_exc()
                    found_error = True

            if found_error:
                self.fail("Found one or more errors during tear-down")

    @classmethod
    def set_copy_path(cls):
        cls.CACHED_COPY_PATH = HsBase.DEFAULT_COPY_PATH
        HsBase.set_default_copy_path(tempfile.mkdtemp())

    @classmethod
    def restore_copy_path(cls):
        if cls.CACHED_COPY_PATH is not None:
            try:
                if HsBase.DEFAULT_COPY_PATH != cls.CACHED_COPY_PATH:
                    try:
                        shutil.rmtree(HsBase.DEFAULT_COPY_PATH)
                    finally:
                        HsBase.DEFAULT_COPY_PATH = cls.CACHED_COPY_PATH
            finally:
                cls.CACHED_COPY_PATH = None

    def test_bad_dir_name(self):
        sender = FailableSender()
        self.set_sender(sender)

        # initialize HitSpool file parameters
        firstnum = 11
        numfiles = 3

        # create fake directory paths
        hsdir = MockHitspool.create_copy_files("BadDir", "12345678_987654",
                                               "ichub01", firstnum, numfiles,
                                               real_stuff=True)
        usrdir = os.path.join(MockHitspool.COPY_DIR, "UserCopy")

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # run it!
        sender.move_to_destination_dir(hsdir, usrdir)

        # files should have been moved!
        self.assertTrue(sender.moved_files(), "Should have moved files")

        # make sure 0MQ communications checked out
        sender.validate()

    def test_no_move(self):
        sender = FailableSender()
        self.set_sender(sender)

        # initialize HitSpool file parameters
        firstnum = 11
        numfiles = 3

        # create fake directory paths
        hsdir = MockHitspool.create_copy_files("ANON", "12345678_987654",
                                               "ichub01", firstnum, numfiles,
                                               real_stuff=True)
        if hsdir.endswith('/'):
            usrdir = os.path.dirname(hsdir[:-1])
        else:
            usrdir = os.path.dirname(hsdir)

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # run it!
        sender.move_to_destination_dir(hsdir, usrdir)

        # make sure no files moved
        self.assertFalse(sender.moved_files(), "Should not have moved files")

        # make sure 0MQ communications checked out
        sender.validate()

    def test_copy_sn_alert(self):
        sender = FailableSender()
        self.set_sender(sender)

        # initialize HitSpool file parameters
        firstnum = 11
        numfiles = 3

        # create fake directory paths
        hsdir = MockHitspool.create_copy_files("HESE", "12345678_987654",
                                               "ichub01", firstnum, numfiles,
                                               real_stuff=True)
        usrdir = os.path.join(MockHitspool.COPY_DIR, "UserCopy")

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # run it!
        sender.move_to_destination_dir(hsdir, usrdir)

        # files should have been moved!
        self.assertTrue(sender.moved_files(), "Should have moved files")

        # make sure 0MQ communications checked out
        sender.validate()

    def test_real_copy_sn_alert(self):
        sender = MySender(verbose=False)
        self.set_sender(sender)

        req = MockRequestBuilder(None, MockRequestBuilder.SNDAQ, None, None,
                                 None, "12345678_987654", "ichub01", 11, 3)

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # run it!
        sender.move_to_destination_dir(req.hsdir, req.destdir)

        req.check_files()

        # make sure 0MQ communications checked out
        sender.validate()

    def test_spade_data_nonstandard_prefix(self):
        sender = FailableSender()
        self.set_sender(sender)

        # initialize directory parts
        category = "SomeCategory"
        timetag = "12345678_987654"
        host = "ichub01"

        # initialize HitSpool file parameters
        firstnum = 11
        numfiles = 3

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        mybase = "%s_%s_%s" % (category, timetag, host)
        mytar = "%s%s" % (mybase, HsSender.HsSender.TAR_SUFFIX)
        if HsSender.HsSender.WRITE_META_XML:
            mysem = "%s%s" % (mybase, HsSender.HsSender.META_SUFFIX)
        else:
            mysem = "%s%s" % (mybase, HsSender.HsSender.SEM_SUFFIX)

        # create real directories
        hsdir = MockHitspool.create_copy_files(category, timetag, host,
                                               firstnum, numfiles,
                                               real_stuff=True)

        # clean up test files
        for fnm in (mytar, mysem):
            tmppath = os.path.join(sender.HS_SPADE_DIR, fnm)
            if os.path.exists(tmppath):
                os.unlink(tmppath)

        # run it!
        (tarname, semname) \
            = sender.spade_pickup_data(hsdir, mybase, prefix=category)
        self.assertEqual(mytar, tarname,
                         "Expected tarfile to be named \"%s\" not \"%s\"" %
                         (mytar, tarname))
        self.assertEqual(mysem, semname,
                         "Expected semaphore to be named \"%s\" not \"%s\"" %
                         (mysem, semname))

        # make sure 0MQ communications checked out
        sender.validate()

    def test_spade_data_fail_tar(self):
        sender = FailableSender()
        self.set_sender(sender)

        sender.fail_create_tar_file()

        # initialize directory parts
        category = "SNALERT"
        timetag = "12345678_987654"
        host = "ichub07"

        # initialize HitSpool file parameters
        firstnum = 11
        numfiles = 3

        # create bad directory name
        hsdir = MockHitspool.create_copy_files(category, timetag, host,
                                               firstnum, numfiles,
                                               real_stuff=False)

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # add all expected log messages
        self.expect_log_message("Fake Tar Error")
        self.expect_log_message("Please put the data manually in the SPADE"
                                " directory. Use HsSpader.py, for example.")

        # run it!
        result = sender.spade_pickup_data(hsdir, "ignored", prefix=category)
        self.assertIsNone(result, "spade_pickup_data() should return None,"
                          " not %s" % str(result))

        # make sure 0MQ communications checked out
        sender.validate()

    def test_spade_data_fail_move(self):
        sender = FailableSender()
        self.set_sender(sender)

        sender.fail_move_file()

        # initialize directory parts
        category = "SNALERT"
        timetag = "12345678_987654"
        host = "ichub07"

        # initialize HitSpool file parameters
        firstnum = 11
        numfiles = 3

        # create bad directory name
        hsdir = MockHitspool.create_copy_files(category, timetag, host,
                                               firstnum, numfiles,
                                               real_stuff=False)

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # add all expected log messages
        self.expect_log_message("Fake Move Error")
        self.expect_log_message("Please put the data manually in the SPADE"
                                " directory. Use HsSpader.py, for example.")

        # run it!
        result = sender.spade_pickup_data(hsdir, "ignored", prefix=category)
        self.assertIsNone(result, "spade_pickup_data() should return None,"
                          " not %s" % str(result))

        # make sure 0MQ communications checked out
        sender.validate()

    def test_spade_data_fail_sem(self):
        sender = FailableSender()
        self.set_sender(sender)

        sender.fail_create_sem_file()

        # initialize directory parts
        category = "SNALERT"
        timetag = "12345678_987654"
        host = "ichub07"

        # initialize HitSpool file parameters
        firstnum = 11
        numfiles = 3

        # create bad directory name
        hsdir = MockHitspool.create_copy_files(category, timetag, host,
                                               firstnum, numfiles,
                                               real_stuff=False)

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # add all expected log messages
        self.expect_log_message("Fake Touch Error")
        self.expect_log_message("Please put the data manually in the SPADE"
                                " directory. Use HsSpader.py, for example.")

        # run it!
        result = sender.spade_pickup_data(hsdir, "ignored", prefix=category)
        self.assertIsNone(result, "spade_pickup_data() should return None,"
                          " not %s" % str(result))

        # make sure 0MQ communications checked out
        sender.validate()

    def test_spade_pickup_data(self):
        sender = MySender(verbose=False)
        self.set_sender(sender)

        # initialize directory parts
        category = "SNALERT"
        timetag = "12345678_987654"
        host = "ichub01"

        # initialize HitSpool file parameters
        firstnum = 11
        numfiles = 3

        # create real directories
        hsdir = MockHitspool.create_copy_files(category, timetag, host,
                                               firstnum, numfiles,
                                               real_stuff=True)

        # set SPADE path to something which exists everywhere
        sender.HS_SPADE_DIR = tempfile.mkdtemp(prefix="SPADE_")

        mybase = "%s_%s_%s" % (category, timetag, host)
        mytar = "HS_%s%s" % (mybase, HsSender.HsSender.TAR_SUFFIX)
        if HsSender.HsSender.WRITE_META_XML:
            mysem = "HS_%s%s" % (mybase, HsSender.HsSender.META_SUFFIX)
        else:
            mysem = "HS_%s%s" % (mybase, HsSender.HsSender.SEM_SUFFIX)

        # create intermediate directory
        movetop = tempfile.mkdtemp(prefix="Intermediate_")
        movedir = os.path.join(movetop, mybase)
        os.makedirs(movedir)

        # copy hitspool files to intermediate directory
        shutil.copytree(hsdir, os.path.join(movedir,
                                            os.path.basename(hsdir)))

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # add all expected log messages

        # clean up test files
        for fnm in (mytar, mysem):
            tmppath = os.path.join(sender.HS_SPADE_DIR, fnm)
            if os.path.exists(tmppath):
                os.unlink(tmppath)

        # run it!
        (tarname, semname) = sender.spade_pickup_data(movedir, mybase,
                                                      prefix=category)
        self.assertEqual(mytar, tarname,
                         "Expected tarfile to be named \"%s\" not \"%s\"" %
                         (mytar, tarname))
        self.assertEqual(mysem, semname,
                         "Expected semaphore to be named \"%s\" not \"%s\"" %
                         (mysem, semname))

        sempath = os.path.join(sender.HS_SPADE_DIR, semname)
        self.assertTrue(os.path.exists(sempath),
                        "Semaphore file %s was not created" % sempath)

        tarpath = os.path.join(sender.HS_SPADE_DIR, tarname)
        self.assertTrue(tarfile.is_tarfile(tarpath),
                        "Tar file %s was not created" % tarpath)

        # read in contents of tarfile
        tar = tarfile.open(tarpath)
        names = []
        for fnm in tar.getnames():
            if fnm == mybase:
                continue
            if fnm.startswith(mybase):
                fnm = fnm[len(mybase)+1:]
            names.append(fnm)
        tar.close()

        # validate the list
        MockRequestBuilder.check_hitspool_file_list(names, firstnum, numfiles)

        # make sure 0MQ communications checked out
        sender.validate()

    def test_main_loop_no_msg(self):
        sender = MySender(verbose=False)
        self.set_sender(sender)

        # initialize message
        no_msg = None

        # add all expected JSON messages
        sender.reporter.add_incoming(no_msg)

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # run it!
        if sender.mainloop():
            self.fail("Succeeded after processing no messages")

        # wait for message to be processed
        sender.wait_for_idle()

        # make sure 0MQ communications checked out
        sender.validate()

    def test_main_loop_str_msg(self):
        sender = MySender(verbose=False)
        self.set_sender(sender)

        # initialize message
        snd_msg = json.dumps("abc")

        # add all expected JSON messages
        sender.reporter.add_incoming(snd_msg)

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # run it!
        try:
            if sender.mainloop():
                self.fail("'str' message was accepted")
        except HsException as hse:
            hsestr = str(hse)
            if hsestr.find("Received ") < 0 or \
               hsestr.find(", not dictionary") < 0:
                self.fail("Unexpected exception: " + hsestr)

        # wait for message to be processed
        sender.wait_for_idle()

        # make sure 0MQ communications checked out
        sender.validate()

    def test_main_loop_no_request_id(self):
        sender = MySender(verbose=False)
        self.set_sender(sender)

        # initialize message
        rcv_msg = {
            "msgtype": "rsync_sum",
            "start_time": None,
            "stop_time": None,
        }

        # add all expected JSON messages
        sender.reporter.add_incoming(rcv_msg)

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # run it!
        try:
            if sender.mainloop():
                self.fail("Message with no request ID was accepted")
        except HsException as hse:
            hsestr = str(hse)
            if hsestr.find("No request ID found in ") < 0:
                self.fail("Unexpected exception: " + hsestr)

        # wait for message to be processed
        sender.wait_for_idle()

        # make sure 0MQ communications checked out
        sender.validate()

    def test_main_loop_incomplete_msg(self):
        sender = MySender(verbose=False)
        self.set_sender(sender)

        # initialize message
        rcv_msg = {"msgtype": "rsync_sum", "request_id": "incomplete"}

        # add all expected JSON messages
        sender.reporter.add_incoming(rcv_msg)

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # run it!
        try:
            if sender.mainloop():
                self.fail("Bad message was accepted")
        except HsException as hse:
            hsestr = str(hse)
            if hsestr.find("Dictionary is missing start_") < 0:
                self.fail("Unexpected exception: " + hsestr)

        # wait for message to be processed
        sender.wait_for_idle()

        # make sure 0MQ communications checked out
        sender.validate()

    def test_main_loop_unknown_msg(self):
        sender = MySender(verbose=False)
        self.set_sender(sender)

        # initialize message
        rcv_msg = {
            "msgtype": "xxx",
            "request_id": None,
            "username": None,
            "start_time": None,
            "stop_time": None,
            "copy_dir": None,
            "destination_dir": None,
            "prefix": None,
            "extract": None,
            "host": None,
            "version": None,
        }

        # add all expected JSON messages
        sender.reporter.add_incoming(rcv_msg)

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # add all expected log messages
        self.expect_log_message(re.compile("Received bad message .*"))

        # run it!
        if sender.mainloop():
            self.fail("Bad message was accepted")

        # wait for message to be processed
        sender.wait_for_idle()

        # make sure 0MQ communications checked out
        sender.validate()

    def test_main_loop_bad_hubs(self):
        sender = MySender(verbose=False)
        self.set_sender(sender)

        # initialize message
        rcv_msg = {
            "msgtype": HsMessage.INITIAL,
            "request_id": "BadHubs",
            "username": "abc",
            "start_ticks": 0,
            "stop_ticks": int(1E10),
            "copy_dir": None,
            "destination_dir": None,
            "prefix": None,
            "extract": None,
            "host": None,
            "hubs": "not_a_hub",
            "version": HsMessage.CURRENT_VERSION,
        }

        # add all expected JSON messages
        sender.reporter.add_incoming(rcv_msg)

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # add all expected log messages
        self.expect_log_message(re.compile(r"Received bad message .*"))

        # run it!
        if sender.mainloop():
            self.fail("Message with bad 'hubs' entry was accepted")

        # wait for message to be processed
        sender.wait_for_idle()

        # make sure 0MQ communications checked out
        sender.validate()

    def test_main_loop_no_init_just_success(self):
        sender = MySender(verbose=False)
        self.set_sender(sender)

        # expected start/stop times
        start_ticks = 98765432100000
        stop_ticks = 98899889980000

        req = MockRequestBuilder("MnLoopNIJS", MockRequestBuilder.SNDAQ, None,
                                 start_ticks, stop_ticks, "12345678_987654",
                                 "ichub01", 11, 3)

        msgtype = HsMessage.DONE
        req.add_request(sender.reporter, msgtype)

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # add all expected log messages
        self.expect_log_message("Received unexpected %s message from"
                                " %s for Req#%s (no active request)" %
                                (msgtype, req.host, req.req_id))

        self.expect_log_message("Request %s was not initialized (received %s"
                                " from %s)" % (req.req_id, msgtype, req.host))
        self.expect_log_message("Saw %s message for request %s host %s without"
                                " a START message" % (msgtype, req.req_id,
                                                      req.host))

        req.add_i3live_message(sender.i3socket, HsUtil.STATUS_SUCCESS,
                               success="1")

        # run it!
        if not sender.mainloop():
            self.fail("Message should not have returned error")

        # wait for message to be processed
        sender.wait_for_idle()

        # make sure expected files were copied
        req.check_files()

        # make sure 0MQ communications checked out
        sender.validate()

    def test_main_loop_multi_request(self):
        sender = MySender(verbose=False)
        self.set_sender(sender)

        # expected start/stop times
        start_ticks = 98765432100000
        stop_ticks = 98899889980000

        # request details
        req_id = "MnLoopMReq"
        req_type = MockRequestBuilder.SNDAQ
        username = "xxx"
        timetag = "12345678_987654"

        # create two requests
        req01 = MockRequestBuilder(req_id, req_type, username, start_ticks,
                                   stop_ticks, timetag, "ichub01", 11, 3)
        req86 = MockRequestBuilder(req_id, req_type, username, start_ticks,
                                   stop_ticks, timetag, "ichub86", 11, 3)

        # add initial message
        req01.add_request(sender.reporter, HsMessage.INITIAL)

        # add initial message for Live
        req01.add_i3live_message(sender.i3socket, HsUtil.STATUS_QUEUED)

        # add start messages
        req01.add_request(sender.reporter, HsMessage.STARTED)
        req86.add_request(sender.reporter, HsMessage.STARTED)

        # initialize some notification data
        notify_hdr = 'DATA REQUEST HsInterface Alert: %s' % sender.cluster
        notify_lines = [
            'Start: %s' % DAQTime.ticks_to_utc(start_ticks),
            'Stop: %s' % DAQTime.ticks_to_utc(stop_ticks),
            '(no possible leapseconds applied)',
        ]
        notify_pat = re.compile(r".*" + re.escape("\n".join(notify_lines)),
                                flags=re.MULTILINE)

        sender.i3socket.add_generic_email(HsConstants.ALERT_EMAIL_DEV,
                                          notify_hdr, notify_pat, prio=1)

        # add hub01 messages
        req01.add_i3live_message(sender.i3socket, HsUtil.STATUS_IN_PROGRESS)
        req01.add_request(sender.reporter, HsMessage.DONE)

        # add hub86 messages
        req86.add_request(sender.reporter, HsMessage.DONE)
        req86.add_i3live_message(sender.i3socket, HsUtil.STATUS_SUCCESS,
                                 success="1,86")

        # don't check DEBUG/INFO log messages
        self.setLogLevel(logging.WARN)

        # run it!
        while sender.reporter.has_input:
            if not sender.mainloop():
                self.fail("Unexpected failure")

        # wait for message to be processed
        sender.wait_for_idle()

        req01.check_files()
        req86.check_files()

        # make sure 0MQ communications checked out
        sender.validate()


if __name__ == '__main__':
    unittest.main()
