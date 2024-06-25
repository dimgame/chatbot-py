# -*- coding: utf-8 -*-
# ==============================================================================
# MIT License
#
# Copyright (c) 2024 Albert Moky
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

# import random
import threading
from typing import Optional, Set, List, Dict

from dimples import DateTime, URI
from dimples import ID
from dimples import Content
from dimples import TextContent
from dimples import CommonFacebook

from ...utils import Log
from ...utils import Singleton, Runner
from ...chat import ChatRequest
from ...chat import ChatBox, VideoBox, ChatClient
from ...chat.base import get_nickname
from ...client import Emitter
from ...client import Monitor

from .engine import Task, Engine
from .engine import KeywordManager
from .tvscan import TVScan, LiveConfig


class SearchBox(VideoBox):
    """ Chat Box """

    def __init__(self, identifier: ID, facebook: CommonFacebook, engines: List[Engine]):
        super().__init__(identifier=identifier, facebook=facebook)
        self.__engines = engines
        self.__task: Optional[Task] = None
        # TODO: LiveConfig for TV channels
        config = LiveConfig(info={
            'tvbox': {
                'sources': [
                    TVScan.INDEX_URI,
                ],
            }
        })
        self.__tv = TVScan(config=config)

    def _cancel_task(self):
        task = self.__task
        if task is not None:
            self.__task = None
            self.warning(msg='cancelling task')
            task.cancel()

    def _new_task(self, keywords: str, request: ChatRequest) -> Task:
        task = Task(keywords=keywords, request=request, box=self)
        self._cancel_task()
        self.__task = task
        return task

    @property
    def service(self) -> str:
        return self.__class__.__name__

    # Override
    async def _send_content(self, content: Content, receiver: ID):
        emitter = Emitter()
        return await emitter.send_content(content=content, receiver=receiver)

    # Override
    async def _ask_question(self, prompt: str, content: TextContent, request: ChatRequest):
        #
        #  0. check group
        #
        sender = request.envelope.sender
        group = request.content.group
        nickname = await get_nickname(identifier=sender, facebook=self.facebook)
        source = '"%s" %s' % (nickname, sender)
        if group is not None:
            name = await get_nickname(identifier=group, facebook=self.facebook)
            if name is None or len(name) == 0:
                source += ' (%s)' % group
            else:
                source += ' (%s)' % name
        self.info(msg='[SEARCHING] received prompt "%s" from %s' % (prompt, source))
        #
        #  1. check keywords
        #
        keywords = prompt.strip()
        kw_len = len(keywords)
        if kw_len == 0:
            return
        else:
            self._cancel_task()
            # save command in history
            his_man = HistoryManager()
            his_man.add_command(cmd=keywords, when=request.time, sender=sender, group=group)
        # system commands
        if kw_len == 6 and keywords.lower() == 'cancel':
            return
        elif kw_len == 4 and keywords.lower() == 'stop':
            return
        elif kw_len == 12 and keywords.lower() == 'show history':
            await _respond_history(history=his_man.commands, request=request, box=self)
            return
        #
        #  2. search
        #
        task = self._new_task(keywords=keywords, request=request)
        if kw_len == 11 and keywords.lower() == 'tv channels':
            tv = self.__tv
            tv.clear_caches()
            coro = tv.search(task=task)
        elif kw_len == 19 and keywords.lower() == 'live stream sources':
            tv = self.__tv
            tv.clear_caches()
            live_urls = await tv.get_live_urls()
            await _respond_live_urls(live_urls=live_urls, request=request, box=self)
            return
        else:
            coro = self._search(task=task)
        # searching in background
        thr = Runner.async_thread(coro=coro)
        thr.start()

    async def _search(self, task: Task):
        all_engines = self.__engines
        count = len(all_engines)
        if count == 0:
            self.error(msg='search engines not set')
            return False
        monitor = Monitor()
        failed = 0
        index = 0
        #
        #  1. try to search by each engine
        #
        while index < count:
            engine = all_engines[index]
            try:
                code = await engine.search(task=task)
            except Exception as error:
                self.error(msg='failed to search: %s, %s, error: %s' % (task, engine, error))
                code = -500
            # check return code
            if code > 0:
                # success
                monitor.report_success(service=self.service, agent=engine.agent)
                break
            elif code == Engine.CANCELLED_CODE:  # code == -205:
                # cancelled
                return False
            elif code < 0:  # code in [-404, -500]:
                self.error(msg='search error from engine: %d %s' % (code, engine))
                monitor.report_failure(service=self.service, agent=engine.agent)
                failed += 1
            index += 1
        #
        #  2. check result
        #
        if index == count:
            key_man = KeywordManager()
            await _respond_204(history=key_man.keywords, keywords=task.keywords, request=task.request, box=self)
        if failed == count:
            # failed to get answer
            monitor.report_crash(service=self.service)
            return False
        elif 0 < index < count:
            # move this handler to the front
            engine = self.__engines.pop(index)
            self.__engines.insert(0, engine)
            self.warning(msg='move engine position: %d, %s' % (index, engine))
        return True


async def _respond_204(history: List[str], keywords: str, request: ChatRequest, box: VideoBox):
    if history is None:
        history = []
    text = 'No contents for **"%s"**, you can try the following keywords:\n' % keywords
    text += '\n----\n'
    for his in history:
        text += '- **%s**\n' % his
    text += '\n'
    text += 'You can also input this command to scan TV channels:\n'
    text += '\n- **TV channels**'
    return await box.respond_markdown(text=text, request=request)


async def _respond_history(history: List[Dict], request: ChatRequest, box: VideoBox):
    text = 'Search history:\n'
    text += '| From | Keyword | Time |\n'
    text += '|------|---------|------|\n'
    for his in history:
        sender = his.get('sender')
        group = his.get('group')
        when = his.get('when')
        cmd = his.get('cmd')
        assert sender is not None and cmd is not None, 'history error: %s' % his
        sender = ID.parse(identifier=sender)
        group = ID.parse(identifier=group)
        user = '**"%s"**' % await box.get_name(identifier=sender)
        if group is not None:
            user += ' (%s)' % await box.get_name(identifier=group)
        text += '| %s | %s | %s |\n' % (user, cmd, when)
    return await box.respond_markdown(text=text, request=request)


async def _respond_live_urls(live_urls: Set[URI], request: ChatRequest, box: VideoBox):
    count = len(live_urls)
    text = 'Live Stream Sources:\n'
    text += '\n----\n'
    for url in live_urls:
        text += '- [%s](%s#lives.txt "LIVE")\n' % (url, url)
    text += '\n----\n'
    text += 'Total %d source(s).' % count
    # search tag
    tag = request.content.get('tag')
    cid = request.identifier
    Log.info(msg='respond %d sources with tag %s to %s' % (count, tag, cid))
    return await box.respond_markdown(text=text, request=request, muted='yes', extra={
        'app': 'chat.dim.tvbox',
        'mod': 'lives',
        'act': 'respond',
        'tag': tag,
        'lives': list(live_urls),
    })


@Singleton
class HistoryManager:

    MAX_LENGTH = 50

    def __init__(self):
        super().__init__()
        self.__commands: List[Dict] = []
        self.__lock = threading.Lock()

    @property
    def commands(self) -> List[Dict]:
        with self.__lock:
            return self.__commands.copy()

    def add_command(self, cmd: str, when: DateTime, sender: ID, group: Optional[ID]):
        with self.__lock:
            self.__commands.append({
                'sender': sender,
                'group': group,
                'when': when,
                'cmd': cmd,
            })


class SearchClient(ChatClient):

    def __init__(self, facebook: CommonFacebook):
        super().__init__()
        self.__facebook = facebook
        self.__engines: List[Engine] = []

    def add_engine(self, engine: Engine):
        self.__engines.append(engine)

    # Override
    def _new_box(self, identifier: ID) -> Optional[ChatBox]:
        facebook = self.__facebook
        # copy engines in random order
        engines = self.__engines.copy()
        # count = len(engines)
        # if count > 1:
        #     engines = random.sample(engines, count)
        return SearchBox(identifier=identifier, facebook=facebook, engines=engines)
