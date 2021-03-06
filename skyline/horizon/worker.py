from __future__ import division
# from os import kill, system
from os import kill
from redis import StrictRedis, WatchError
from multiprocessing import Process
try:
    from Queue import Empty
except ImportError:
    from queue import Empty
from msgpack import packb
from time import time, sleep

import traceback
import logging
# import socket

# import sys
import os.path
from os import remove as os_remove

# @added 20190130 - Task #2690: Test Skyline on Python-3.6.7
#                   Branch #3262: py3
from sys import version_info

import settings
from skyline_functions import send_graphite_metric

parent_skyline_app = 'horizon'
child_skyline_app = 'worker'
skyline_app_logger = '%sLog' % parent_skyline_app
logger = logging.getLogger(skyline_app_logger)
skyline_app = '%s.%s' % (parent_skyline_app, child_skyline_app)
skyline_app_logfile = '%s/%s.log' % (settings.LOG_PATH, parent_skyline_app)
skyline_app_loglock = '%s.lock' % skyline_app_logfile
skyline_app_logwait = '%s.wait' % skyline_app_logfile

try:
    SERVER_METRIC_PATH = '.%s' % settings.SERVER_METRICS_NAME
    if SERVER_METRIC_PATH == '.':
        SERVER_METRIC_PATH = ''
except:
    SERVER_METRIC_PATH = ''

skyline_app_graphite_namespace = 'skyline.%s%s.%s' % (
    parent_skyline_app, SERVER_METRIC_PATH, child_skyline_app)

LOCAL_DEBUG = False

# @added 20170319 - Feature #1978: worker - DO_NOT_SKIP_LIST
try:
    DO_NOT_SKIP_LIST = settings.DO_NOT_SKIP_LIST
except:
    DO_NOT_SKIP_LIST = []

# @added 20190130 - Task #2690: Test Skyline on Python-3.6.7
#                   Branch #3262: py3
python_version = int(version_info[0])


class Worker(Process):
    """
    The worker processes chunks from the queue and appends
    the latest datapoints to their respective timesteps in Redis.
    """
    def __init__(self, queue, parent_pid, skip_mini, canary=False):
        super(Worker, self).__init__()
        # @modified 20180519 - Feature #2378: Add redis auth to Skyline and rebrow
        if settings.REDIS_PASSWORD:
            # @modified 20191014 - Bug #3266: py3 Redis binary objects not strings
            #                      Branch #3262: py3
            # self.redis_conn = StrictRedis(password=settings.REDIS_PASSWORD, unix_socket_path=settings.REDIS_SOCKET_PATH)
            self.redis_conn = StrictRedis(password=settings.REDIS_PASSWORD, unix_socket_path=settings.REDIS_SOCKET_PATH, charset='utf-8', decode_responses=True)
        else:
            # self.redis_conn = StrictRedis(unix_socket_path=settings.REDIS_SOCKET_PATH)
            self.redis_conn = StrictRedis(unix_socket_path=settings.REDIS_SOCKET_PATH, charset='utf-8', decode_responses=True)
        self.q = queue
        self.parent_pid = parent_pid
        self.daemon = True
        self.canary = canary
        self.skip_mini = skip_mini

    def check_if_parent_is_alive(self):
        """
        Self explanatory.
        """
        try:
            kill(self.parent_pid, 0)
        except:
            exit(0)

    def in_skip_list(self, metric_name):
        """
        Check if the metric is in SKIP_LIST.

        # @added 20170319 - Feature #1978: worker - DO_NOT_SKIP_LIST
        The SKIP_LIST allows for a string match or a match on dotted elements
        within the metric namespace.

        """

        # @modified 20170319 - Feature #1978: worker - DO_NOT_SKIP_LIST
        # Allow for dotted element matches and DO_NOT_SKIP_LIST
        # for to_skip in settings.SKIP_LIST:
        #    if to_skip in metric_name:
        #        return True
        # return False

        # @added 20190130 - Task #2690: Test Skyline on Python-3.6.7
        #                   Branch #3262: py3
        #                   Bug #3266: py3 Redis binary objects not strings
        if python_version == 3:
            str_metric_name = str(metric_name)
            metric_name = str_metric_name

        metric_namespace_elements = metric_name.split('.')
        process_metric = True

        for to_skip in settings.SKIP_LIST:
            if to_skip in metric_name:
                process_metric = False
                break
            to_skip_namespace_elements = to_skip.split('.')
            elements_matched = set(metric_namespace_elements) & set(to_skip_namespace_elements)
            if len(elements_matched) == len(to_skip_namespace_elements):
                process_metric = False
                break

        if not process_metric:
            for do_not_skip in DO_NOT_SKIP_LIST:
                if do_not_skip in metric_name:
                    process_metric = True
                    break
                do_not_skip_namespace_elements = do_not_skip.split('.')
                elements_matched = set(metric_namespace_elements) & set(do_not_skip_namespace_elements)
                if len(elements_matched) == len(do_not_skip_namespace_elements):
                    process_metric = True
                    break

        if not process_metric:
            # skip
            return True

        return False

    def run(self):
        """
        Called when the process intializes.
        """
        # Log management to prevent overwriting
        # Allow the bin/<skyline_app>.d to manage the log
        if os.path.isfile(skyline_app_logwait):
            try:
                os_remove(skyline_app_logwait)
            except OSError:
                logger.error('error - failed to remove %s, continuing' % skyline_app_logwait)
                pass

        now = time()
        log_wait_for = now + 5
        while now < log_wait_for:
            if os.path.isfile(skyline_app_loglock):
                sleep(.1)
                now = time()
            else:
                now = log_wait_for + 1

        logger.info('starting %s run' % skyline_app)
        if os.path.isfile(skyline_app_loglock):
            logger.error('error - bin/%s.d log management seems to have failed, continuing' % skyline_app)
            try:
                os_remove(skyline_app_loglock)
                logger.info('log lock file removed')
            except OSError:
                logger.error('error - failed to remove %s, continuing' % skyline_app_loglock)
                pass
        else:
            logger.info('bin/%s.d log management done' % skyline_app)

        logger.info('%s :: started worker' % skyline_app)

        FULL_NAMESPACE = settings.FULL_NAMESPACE
        MINI_NAMESPACE = settings.MINI_NAMESPACE
        MAX_RESOLUTION = settings.MAX_RESOLUTION
        full_uniques = '%sunique_metrics' % FULL_NAMESPACE
        mini_uniques = '%sunique_metrics' % MINI_NAMESPACE
        pipe = self.redis_conn.pipeline()

        last_send_to_graphite = time()
        queue_sizes = []

        # python-2.x and python3.x handle while 1 and while True differently
        # while 1:
        running = True
        while running:

            # Make sure Redis is up
            try:
                self.redis_conn.ping()
            except:
                logger.error('%s :: cannot connect to Redis at socket path %s' % (skyline_app, settings.REDIS_SOCKET_PATH))
                sleep(10)
                # @modified 20180519 - Feature #2378: Add redis auth to Skyline and rebrow
                if settings.REDIS_PASSWORD:
                    # @modified 20191014 - Bug #3266: py3 Redis binary objects not strings
                    #                      Branch #3262: py3
                    # self.redis_conn = StrictRedis(password=settings.REDIS_PASSWORD, unix_socket_path=settings.REDIS_SOCKET_PATH)
                    self.redis_conn = StrictRedis(password=settings.REDIS_PASSWORD, unix_socket_path=settings.REDIS_SOCKET_PATH, charset='utf-8', decode_responses=True)
                else:
                    # self.redis_conn = StrictRedis(unix_socket_path=settings.REDIS_SOCKET_PATH)
                    self.redis_conn = StrictRedis(unix_socket_path=settings.REDIS_SOCKET_PATH, charset='utf-8', decode_responses=True)
                pipe = self.redis_conn.pipeline()
                continue

            try:
                # Get a chunk from the queue with a 15 second timeout
                chunk = self.q.get(True, 15)
                # @modified 20170317 - Feature #1978: worker - DO_NOT_SKIP_LIST
                # now = time()
                now = int(time())

                for metric in chunk:

                    # Check if we should skip it
                    if self.in_skip_list(metric[0]):
                        continue

                    # Bad data coming in
                    # @modified 20190130 - Task #2690: Test Skyline on Python-3.6.7
                    #                      Branch #3262: py3
                    #                      Bug #3266: py3 Redis binary objects not strings
                    # if metric[1][0] < now - MAX_RESOLUTION:
                    #     continue
                    try:
                        if int(metric[1][0]) < now - MAX_RESOLUTION:
                            continue
                    except:
                        pass

                    # Append to messagepack main namespace
                    # @modified 20190130 - Task #2690: Test Skyline on Python-3.6.7
                    #                      Branch #3262: py3
                    #                      Bug #3266: py3 Redis binary objects not strings
                    # key = ''.join((FULL_NAMESPACE, metric[0]))
                    key = ''.join((FULL_NAMESPACE, str(metric[0])))
                    if LOCAL_DEBUG:
                        logger.info('debug :: worker :: adding metric Redis key - %s' % str(key))

                    # @modified 20190130 - Task #2690: Test Skyline on Python-3.6.7
                    #                      Branch #3262: py3
                    #                      Bug #3266: py3 Redis binary objects not strings
                    # pipe.append(key, packb(metric[1]))
                    # pipe.sadd(full_uniques, key)
                    try:
                        pipe.append(str(key), packb(metric[1]))
                    except Exception as e:
                        logger.error('%s :: error on pipe.append: %s' % (skyline_app, str(e)))
                    try:
                        # pipe.sadd(full_uniques, key)
                        pipe.sadd(full_uniques, str(key))
                    except Exception as e:
                        logger.error('%s :: error on pipe.sadd: %s' % (skyline_app, str(e)))

                    if not self.skip_mini:
                        # Append to mini namespace
                        # @modified 20190130 - Task #2690: Test Skyline on Python-3.6.7
                        #                      Branch #3262: py3
                        #                      Bug #3266: py3 Redis binary objects not strings
                        # mini_key = ''.join((MINI_NAMESPACE, metric[0]))
                        mini_key = ''.join((MINI_NAMESPACE, str(metric[0])))
                        pipe.append(mini_key, packb(metric[1]))
                        pipe.sadd(mini_uniques, mini_key)

                    # @modified 20190130 - Task #2690: Test Skyline on Python-3.6.7
                    #                      Branch #3262: py3
                    #                      Bug #3266: py3 Redis binary objects not strings
                    # pipe.execute()
                    try:
                        pipe.execute()
                    except Exception as e:
                        logger.error('%s :: error on pipe.execute: %s' % (skyline_app, str(e)))
            except Empty:
                logger.info('%s :: worker queue is empty and timed out' % skyline_app)
            except WatchError:
                logger.error('%s :: WatchError - %s' % (skyline_app, str(key)))
            except NotImplementedError:
                pass
            except Exception as e:
                # @added 20190130 - Task #2690: Test Skyline on Python-3.6.7
                #                   Branch #3262: py3
                #                   Bug #3266: py3 Redis binary objects not strings
                # Added traceback
                logger.error(traceback.format_exc())
                logger.error('%s :: error: %s' % (skyline_app, str(e)))

            # Log progress
            if self.canary:
                logger.info('%s :: queue size at %d' % (skyline_app, self.q.qsize()))
                queue_sizes.append(self.q.qsize())
                # Only send average queue mertics to graphite once per 10 seconds
                now = time()
                last_sent_graphite = now - last_send_to_graphite
                if last_sent_graphite > 10:
                    number_queue_sizes = len(queue_sizes)
                    total_of_queue_sizes = sum(queue_sizes)
                    if total_of_queue_sizes > 0:
                        average_queue_size = total_of_queue_sizes / number_queue_sizes
                    else:
                        average_queue_size = 0
                    logger.info('%s :: total queue size for the last 10 seconds - %s' % (skyline_app, str(total_of_queue_sizes)))
                    logger.info('%s :: total queue values known for the last 10 seconds - %s' % (skyline_app, str(number_queue_sizes)))
                    logger.info('%s :: average queue size for the last 10 seconds - %s' % (skyline_app, str(average_queue_size)))
                    # self.send_graphite_metric('skyline.horizon.' + SERVER_METRIC_PATH + 'queue_size', self.q.qsize())
                    # self.send_graphite_metric('queue_size', average_queue_size)
                    send_metric_name = '%s.queue_size' % skyline_app_graphite_namespace
                    send_graphite_metric(skyline_app, send_metric_name, average_queue_size)

                    # reset queue_sizes and last_sent_graphite
                    queue_sizes = []
                    last_send_to_graphite = time()
