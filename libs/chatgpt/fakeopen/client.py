#! /usr/bin/env python3
# -*- coding: utf-8 -*-
# ==============================================================================
# MIT License
#
# Copyright (c) 2023 Albert Moky
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# ==============================================================================

import threading
import time
import weakref
from typing import Optional, Set

from dimples import ID
from dimples.utils import Singleton
from dimples.utils import Runner, Logging


from ..http import HttpSession
from ..chat import ChatRequest, ChatCallback, ChatTask, ChatTaskPool

from .gpt35 import FakeOpen


#
#   Chat Box
#


class ChatBox(Logging):

    EXPIRES = 36000  # seconds

    def __init__(self, base_url: str, referer: str, auth_token: str, http_session: HttpSession):
        super().__init__()
        gpt = FakeOpen(base_url=base_url, referer=referer, auth_token=auth_token, http_session=http_session)
        self.__gpt = gpt
        self.__expired = time.time() + self.EXPIRES

    def is_expired(self, now: float) -> bool:
        return now > self.__expired

    def __prepare(self) -> bool:
        self.__expired = time.time() + self.EXPIRES
        return True

    def ask(self, question: str) -> Optional[str]:
        if self.__prepare():
            return self.__gpt.ask(question=question)


class ChatBoxPool(Logging):

    def __init__(self):
        super().__init__()
        self.__map = weakref.WeakValueDictionary()  # ID => ChatBox
        self.__boxes: Set[ChatBox] = set()          # Set[ChatBox]
        self.__lock = threading.Lock()
        self.__next_purge_time = 0

    @classmethod
    def __new_box(cls, base_url: str, referer: str, http_session: HttpSession) -> Optional[ChatBox]:
        auth_token = 'Bearer pk-this-is-a-real-free-pool-token-for-everyone'
        return ChatBox(base_url=base_url, referer=referer, auth_token=auth_token, http_session=http_session)

    def get_box(self, identifier: ID, base_url: str, referer: str, http_session: HttpSession) -> Optional[ChatBox]:
        with self.__lock:
            box = self.__map.get(identifier)
            if box is None:
                box = self.__new_box(base_url=base_url, referer=referer, http_session=http_session)
                if box is not None:
                    self.__map[identifier] = box
                    self.__boxes.add(box)
            return box

    def purge(self):
        now = time.time()
        if now < self.__next_purge_time:
            return False
        else:
            self.__next_purge_time = now + 60
        # remove expired box(es)
        with self.__lock:
            boxes = self.__boxes.copy()
            for box in boxes:
                if box.is_expired(now=now):
                    self.__boxes.discard(box)
        return True


@Singleton
class ChatClient(Runner, Logging):

    BASE_URL = 'https://ai.fakeopen.com'
    REFERER_URL = 'https://chat1.geekgpt.org/'

    def __init__(self):
        super().__init__(interval=Runner.INTERVAL_SLOW)
        self.__session = HttpSession(long_connection=True)
        # pools
        self.__box_pool = ChatBoxPool()
        self.__task_pool = ChatTaskPool()

    def request(self, question: str, identifier: ID, callback: ChatCallback):
        request = ChatRequest(question=question, identifier=identifier)
        task = ChatTask(request=request, callback=callback)
        self.__task_pool.add_task(task=task)

    # Override
    def process(self) -> bool:
        task = self.__task_pool.pop_task()
        if task is None:
            # nothing to do now, pure expired boxes
            self.__box_pool.purge()
            return False
        request = task.request
        question = request.question
        identifier = request.identifier
        http_session = self.__session
        base = self.BASE_URL
        referer = self.REFERER_URL
        box = self.__box_pool.get_box(identifier=identifier, base_url=base, referer=referer, http_session=http_session)
        if box is None:
            self.error(msg='failed to get chat box, drop request from %s: "%s"' % (identifier, question))
            return False
        answer = box.ask(question=question)
        if answer is None:
            self.error(msg='failed to get answer, drop request from %s: "%s"' % (identifier, question))
            answer = '{"code": 404, "error": "No response, please try again later."}'
        # OK
        task.chat_response(answer=answer, request=request)
        return True

    def start(self):
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
