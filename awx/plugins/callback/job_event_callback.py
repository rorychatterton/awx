# Copyright (c) 2014 AnsibleWorks, Inc.
# This file is a utility Ansible plugin that is not part of the AWX or Ansible
# packages.  It does not import any code from either package, nor does its
# license apply to Ansible or AWX.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
# Redistributions of source code must retain the above copyright notice, this
# list of conditions and the following disclaimer.
#
#    Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
#    Neither the name of the <ORGANIZATION> nor the names of its contributors
#    may be used to endorse or promote products derived from this software
#    without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

# Python
import datetime
import json
import logging
import os
import sys
import urllib
import urlparse

# Requests / Kombu
try:
    import requests
    from kombu import Connection, Exchange, Queue
except ImportError:
    # If running from an AWX installation, use the local version of requests if
    # if cannot be found globally.
    local_site_packages = os.path.join(os.path.dirname(__file__), '..', '..',
                                       'lib', 'site-packages')
    sys.path.insert(0, local_site_packages)
    import requests
    from kombu import Connection, Exchange, Queue

# Check to see if librabbitmq is installed.
try:
    import librabbitmq
    LIBRABBITMQ_INSTALLED = True
except ImportError:
    LIBRABBITMQ_INSTALLED = False


class TokenAuth(requests.auth.AuthBase):

    def __init__(self, token):
        self.token = token

    def __call__(self, request):
        request.headers['Authorization'] = 'Token %s' % self.token
        return request


class CallbackModule(object):
    '''
    Callback module for logging ansible-playbook job events via the REST API.
    '''

    # These events should never have an associated play.
    EVENTS_WITHOUT_PLAY = [
        'playbook_on_start',
        'playbook_on_stats',
    ]
    # These events should never have an associated task.
    EVENTS_WITHOUT_TASK = EVENTS_WITHOUT_PLAY + [
        'playbook_on_setup',
        'playbook_on_notify',
        'playbook_on_import_for_host',
        'playbook_on_not_import_for_host',
        'playbook_on_no_hosts_matched',
        'playbook_on_no_hosts_remaining',
    ]

    def __init__(self):
        self.job_id = int(os.getenv('JOB_ID'))
        self.base_url = os.getenv('REST_API_URL', '')
        self.auth_token = os.getenv('REST_API_TOKEN', '')
        self.broker_url = os.getenv('BROKER_URL', '')
        self._init_logging()
        # Since we don't yet have a way to confirm publish when using
        # librabbitmq, ensure we use pyamqp even if librabbitmq happens to be
        # installed.
        if LIBRABBITMQ_INSTALLED:
            self.logger.info('Forcing use of pyamqp instead of librabbitmq')
            self.broker_url = self.broker_url.replace('amqp://', 'pyamqp://')

    def _init_logging(self):
        try:
            self.job_callback_debug = int(os.getenv('JOB_CALLBACK_DEBUG', '0'))
        except ValueError:
            self.job_callback_debug = 0
        self.logger = logging.getLogger('awx.plugins.callback.job_event_callback')
        if self.job_callback_debug >= 2:
            self.logger.setLevel(logging.DEBUG)
        elif self.job_callback_debug >= 1:
            self.logger.setLevel(logging.INFO)
        else:
            self.logger.setLevel(logging.WARNING)
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(levelname)-8s %(process)-8d %(message)s')
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.logger.propagate = False

    def __del__(self):
        self._cleanup_connection()

    def _publish_errback(self, exc, interval):
        self.logger.info('Publish Error: %r', exc)

    def _cleanup_connection(self):
        if hasattr(self, 'producer'):
            try:
                #self.logger.debug('Cleanup Producer: %r', self.producer)
                self.producer.cancel()
            except:
                pass
            del self.producer
        if hasattr(self, 'connection'):
            try:
                #self.logger.debug('Cleanup Connection: %r', self.connection)
                self.connection.release()
            except:
                pass
            del self.connection

    def _post_job_event_queue_msg(self, event, event_data):
        if not hasattr(self, 'job_events_exchange'):
            self.job_events_exchange = Exchange('job_events', 'direct',
                                                durable=True)
        if not hasattr(self, 'job_events_queue'):
            self.job_events_queue = Queue('job_events[%d]' % self.job_id,
                                          exchange=self.job_events_exchange,
                                          routing_key=('job_events[%d]' % self.job_id),
                                          auto_delete=True)
        msg = {
            'job_id': self.job_id,
            'event': event,
            'event_data': event_data,
            'created': datetime.datetime.utcnow().isoformat(),
        }
        if self.job_callback_debug:
            msg.update({
                'pid': os.getpid(),
            })
        for retry_count in xrange(4):
            try:
                if not hasattr(self, 'connection_pid'):
                    self.connection_pid = os.getpid()
                if self.connection_pid != os.getpid():
                    self._cleanup_connection()
                if not hasattr(self, 'connection'):
                    self.connection = Connection(self.broker_url, transport_options={'confirm_publish': True})
                    self.logger.debug('New Connection: %r, retry=%d',
                                      self.connection, retry_count)
                if not hasattr(self, 'producer'):
                    channel = self.connection.channel()
                    self.producer = self.connection.Producer(channel, exchange=self.job_events_exchange, serializer='json')
                    self.publish = self.connection.ensure(self.producer, self.producer.publish,
                                                          errback=self._publish_errback,
                                                          max_retries=3, interval_start=1, interval_step=1, interval_max=10)
                    self.logger.debug('New Producer: %r, retry=%d',
                                      self.producer, retry_count)
                self.logger.debug('Publish: %r, retry=%d', msg, retry_count)
                self.publish(msg, exchange=self.job_events_exchange,
                             routing_key=('job_events[%d]' % self.job_id),
                             declare=[self.job_events_queue])
                if event == 'playbook_on_stats':
                    self._cleanup_connection()
                return
            except Exception, e:
                self.logger.info('Publish Exception: %r, retry=%d', e,
                                 retry_count, exc_info=True)
                self._cleanup_connection()
                if retry_count >= 3:
                    raise

    def _post_rest_api_event(self, event, event_data):
        data = json.dumps({
            'event': event,
            'event_data': event_data,
        })
        parts = urlparse.urlsplit(self.base_url)
        if parts.username and parts.password:
            auth = (parts.username, parts.password)
        elif self.auth_token:
            auth = TokenAuth(self.auth_token)
        else:
            auth = None
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        url = urlparse.urlunsplit([parts.scheme,
                                   '%s:%d' % (parts.hostname, port),
                                   parts.path, parts.query, parts.fragment])
        url_path = '/api/v1/jobs/%d/job_events/' % self.job_id
        url = urlparse.urljoin(url, url_path)
        headers = {'content-type': 'application/json'}
        response = requests.post(url, data=data, headers=headers, auth=auth)
        response.raise_for_status()

    def _log_event(self, event, **event_data):
        play = getattr(getattr(self, 'play', None), 'name', '')
        if play and event not in self.EVENTS_WITHOUT_PLAY:
            event_data['play'] = play
        task = getattr(getattr(self, 'task', None), 'name', '')
        if task and event not in self.EVENTS_WITHOUT_TASK:
            event_data['task'] = task
        if self.broker_url:
            self._post_job_event_queue_msg(event, event_data)
        else:
            self._post_rest_api_event(event, event_data)

    def on_any(self, *args, **kwargs):
        pass

    def runner_on_failed(self, host, res, ignore_errors=False):
        self._log_event('runner_on_failed', host=host, res=res,
                        ignore_errors=ignore_errors)

    def runner_on_ok(self, host, res):
        self._log_event('runner_on_ok', host=host, res=res)

    def runner_on_error(self, host, msg):
        self._log_event('runner_on_error', host=host, msg=msg)

    def runner_on_skipped(self, host, item=None):
        self._log_event('runner_on_skipped', host=host, item=item)

    def runner_on_unreachable(self, host, res):
        self._log_event('runner_on_unreachable', host=host, res=res)

    def runner_on_no_hosts(self):
        self._log_event('runner_on_no_hosts')

    def runner_on_async_poll(self, host, res, jid, clock):
        self._log_event('runner_on_async_poll', host=host, res=res, jid=jid,
                        clock=clock)

    def runner_on_async_ok(self, host, res, jid):
        self._log_event('runner_on_async_ok', host=host, res=res, jid=jid)

    def runner_on_async_failed(self, host, res, jid):
        self._log_event('runner_on_async_failed', host=host, res=res, jid=jid)

    def runner_on_file_diff(self, host, diff):
        self._log_event('runner_on_file_diff', host=host, diff=diff)

    def playbook_on_start(self):
        self._log_event('playbook_on_start')

    def playbook_on_notify(self, host, handler):
        self._log_event('playbook_on_notify', host=host, handler=handler)

    def playbook_on_no_hosts_matched(self):
        self._log_event('playbook_on_no_hosts_matched')

    def playbook_on_no_hosts_remaining(self):
        self._log_event('playbook_on_no_hosts_remaining')

    def playbook_on_task_start(self, name, is_conditional):
        self._log_event('playbook_on_task_start', name=name,
                        is_conditional=is_conditional)

    def playbook_on_vars_prompt(self, varname, private=True, prompt=None,
                                encrypt=None, confirm=False, salt_size=None,
                                salt=None, default=None):
        self._log_event('playbook_on_vars_prompt', varname=varname,
                        private=private, prompt=prompt, encrypt=encrypt,
                        confirm=confirm, salt_size=salt_size, salt=salt,
                        default=default)

    def playbook_on_setup(self):
        self._log_event('playbook_on_setup')

    def playbook_on_import_for_host(self, host, imported_file):
        # don't care about recording this one
        # self._log_event('playbook_on_import_for_host', host=host,
        #                imported_file=imported_file)
        pass

    def playbook_on_not_import_for_host(self, host, missing_file):
        # don't care about recording this one
        #self._log_event('playbook_on_not_import_for_host', host=host,
        #                missing_file=missing_file)
        pass

    def playbook_on_play_start(self, pattern):
        self._log_event('playbook_on_play_start', pattern=pattern)

    def playbook_on_stats(self, stats):
        d = {}
        for attr in ('changed', 'dark', 'failures', 'ok', 'processed', 'skipped'):
            d[attr] = getattr(stats, attr)
        self._log_event('playbook_on_stats', **d)
