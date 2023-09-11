import asyncio
import atexit
import json
import os
import re
import signal
import tempfile
from functools import partial
from io import StringIO
from pathlib import Path
from subprocess import PIPE
from time import time

import pexpect
from pexpect.exceptions import EOF
from pexpect.exceptions import TIMEOUT

from . import utils
from .telegram import Telegram
from telminal.values import ACTIVE_TASKS_MSG
from telminal.values import BROWSER_ERROR_MSG
from telminal.values import EMPTY_TASKS_MSG
from telminal.values import PROCESS_INFO_MSG

temp_dir = tempfile.TemporaryDirectory(prefix="telminal-")
TEMP_PATH = Path(temp_dir.name)


class TProcess:
    def __init__(self, command: str, request_id: int) -> None:
        self.command = command
        self.request_id = request_id
        self._buffer = StringIO()
        self.is_running = None
        self.is_partial = None
        self.start_time = None
        self.run_time = 0
        self._new_data = None
        self.response_id = None
        self._last_message = ""
        self.buttons = None
        self.is_interactive_process = False

    def run(self, stream=True) -> None:
        self._process = pexpect.spawn("/bin/bash", ["-c", self.command], timeout=None)
        self.pid = self._process.pid
        self.is_running = True
        self.start_time = time()
        if stream is True:
            asyncio.create_task(self.stream())

    def done(self):
        self.is_running = False
        self.done_time = time()

    def terminate(self):
        self._process.terminate()
        self.done()

    async def stream(self) -> None:
        while True:
            try:
                line = self._process.read_nonblocking(size=1000, timeout=0)
                self._buffer.write(line.decode("utf-8"))
                self._new_data = True
            except EOF:
                self.done()
                break
            except TIMEOUT:
                pass
            finally:
                self.run_time = int(time() - self.start_time)
                await asyncio.sleep(00.1)

    @property
    def full_output(self) -> str:
        self._buffer.seek(0)
        return self._buffer.read()

    @property
    def media_output(self) -> str:
        return Telegram.media_strip(self.full_output)

    @property
    def html(self):
        file = TEMP_PATH / f"{self.pid}.html"
        with open(file, "w", encoding="utf-8") as html:
            html.write(
                utils.HTML_TEMPLATE.format(
                    title=f"{self.pid} -> {self.command}",
                    data=self.full_output.replace("`", r"\`"),
                )
            )
        return file

    @property
    def last_message(self):
        return self._last_message

    @last_message.setter
    def last_message(self, last_message: str):
        self._last_message = last_message
        self._new_data = False
        # TODO seprate this stuff
        self.last_update_time = time()

    def push(self, command):
        if command.startswith("^") and len(command) == 2:
            self._process.sendcontrol(command[-1])
        else:
            for index, word in enumerate(command.split("\n")):
                if index != 0:
                    # for each \n send an enter
                    self._process.sendcontrol("m")
                self._process.sendcontrol("m") if not word else self._process.send(word)

    def update_buttons(self):
        from telethon import Button

        if self.is_running:
            interact_switch_text = "Interactive mode"
            if self.is_interactive_process:
                interact_switch_text = "Exit interactive mode"

            buttons = [
                [Button.inline("💡 Info 💡", data=f"info&{self.pid}")],
                [Button.inline("↩️ Enter ↩️", data=f"enter&{self.pid}")],
                [Button.inline(interact_switch_text, data=f"interact&{self.pid}")],
                [Button.inline("🛑 Terminate 🛑", data=f"terminate&{self.pid}")],
                [Button.inline("🌐 HTML 🌐", data=f"html&{self.pid}")],
            ]
        else:
            buttons = [
                [Button.inline("💡 Info 💡", data=f"info&{self.pid}")],
                [Button.inline("🌐 HTML 🌐", data=f"html&{self.pid}")],
            ]

        if self.buttons != buttons:
            self.buttons = buttons
            return True
        return False

    def has_new_state(self, new_buttons, new_output):
        return new_buttons or (new_output and self.last_message != new_output)

    def __str__(self) -> str:
        status = "🔄 Running" if self.is_running else "✔️ Done"
        start_at = utils.timestamp_to_readable(self.start_time)
        last_update = utils.timestamp_to_readable(self.last_update_time)
        run_time = utils.seconds_to_readable(self.run_time)
        return PROCESS_INFO_MSG.format(
            self.pid, status, start_at, last_update, run_time
        )


class Telminal:
    all_processes = {}
    all_progress_callback = {}

    PROCESS_CLEANER_DELAY = 100
    PROCESS_OUTPUT_LIFE_TIME = 60
    AUTH_TOKEN_EXPIRE_TIME = 60

    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        token: str,
        admins: list = None,
        session_name: str = "telminal",
    ) -> None:
        self.interactive_process = None
        self._api_id = api_id
        self._api_hash = api_hash
        self._token = token
        self.session_name = session_name
        self.admins = admins if admins is not None else []
        self._render = True
        self._watch_tasks = {}
        self._last_process = None

    @classmethod
    async def process_cleaner(cls):
        """Check and clean dead processes periodically"""
        while True:
            for pid, process in cls.all_processes.copy().items():
                if (
                    not process.is_running
                    and int(time() - process.done_time) > cls.PROCESS_OUTPUT_LIFE_TIME
                ):
                    del cls.all_processes[pid]
                    utils.silent_file_remover(TEMP_PATH / f"{pid}.html")
                    utils.silent_file_remover(TEMP_PATH / f"{pid}.png")
            await asyncio.sleep(cls.PROCESS_CLEANER_DELAY)

    def check_permission(func):
        async def inner(self, event):
            if not self.admins and event.message.message == self._authentication_token:
                self.admins.append(event.sender_id)
                await self.setup_browser()
                await self.bot.send_message(
                    event.chat_id, "Welcome to telminal, send your commands"
                )
                del self._authentication_token
                return

            if event.sender_id not in self.admins:
                return
            await func(self, event)

        return inner

    async def setup_browser(self):
        from pyppeteer import launch

        try:
            self.browser = await launch(
                options={
                    "args": [
                        "--disable-gpu",
                        "--disable-dev-shm-usage",
                        "--disable-setuid-sandbox",
                        "--no-sandbox",
                    ],
                    "headless": True,
                    "autoClose": False,
                }
            )
        except Exception as e:
            await self.bot.send_message(
                self.admins[0],
                BROWSER_ERROR_MSG.format(error=e),
                parse_mode="html",
            )
            self.browser = None

    def _token_timeout(self, *_args):
        signal.alarm(0) if self.admins else self._generate_authentication_token()

    def _generate_authentication_token(self):
        import uuid
        import sys

        self._authentication_token = uuid.uuid4().hex[:7]
        sys.stdout.write(f"New token generated...: {self._authentication_token}\n\n")
        signal.signal(signal.SIGALRM, self._token_timeout)
        signal.alarm(Telminal.AUTH_TOKEN_EXPIRE_TIME)

    def find_process_by_event(func):
        async def inner(self, event):
            pid = int(event.data.decode().split("&")[-1])
            process = Telminal.all_processes.get(pid)
            if process is None:
                await event.answer("this process not exist anymore", alert=True)
                # clear button
                await self.bot.edit_message(event.chat_id, message_id=event.message_id)
                return

            await func(self, event, process)

        return inner

    async def start(self):
        """Run telminal instance by calling this method"""
        self.bot = Telegram(
            api_id=self._api_id,
            api_hash=self._api_hash,
            token=self._token,
            session_name=self.session_name,
        )
        from telethon import events

        handlers = {
            self._all_messages_handler: events.NewMessage(incoming=True),
            self._terminate_handler: events.CallbackQuery(pattern=r"terminate&\d+"),
            self._html_handler: events.CallbackQuery(pattern=r"html&\d+"),
            self._interactive_handler: events.CallbackQuery(pattern=r"interact&\d+"),
            self._inline_query_handler: events.InlineQuery(),
            self._cancell_download_handler: events.CallbackQuery(pattern=r"removeme"),
            self._confirm_download_handler: events.CallbackQuery(
                pattern=r"savefile&.+"
            ),
            self._enter_handler: events.CallbackQuery(pattern=r"enter&\d+"),
            self._cancell_task_handler: events.CallbackQuery(
                pattern=r"cancell_task&\d+"
            ),
            self._info_handler: events.CallbackQuery(pattern=r"info&\d+"),
        }
        asyncio.shield(Telminal.process_cleaner())
        await self.bot.start(handlers)
        if not self.admins:
            self._generate_authentication_token()
        atexit.register(self._exit_jobs)
        await self.bot.run_until_disconnected()

    async def render_xtermjs(self, process: TProcess):
        """Returns parsed output and screenshot of a process"""

        output = process.media_output
        image = None

        if getattr(self, "browser", None) is None or self._render is False:
            return output, image

        try:
            page = await self.browser.newPage()
            await page.goto((process.html).as_uri())
            path = TEMP_PATH / f"{process.pid}.png"
            await page.screenshot({"path": path, "fullPage": True})

            await page.evaluate("term.selectAll()")
            output = await page.evaluate("term.getSelection()")
            output = Telegram.media_strip(output)
            image = path
            await page.close()

        except Exception:
            # TODO log this
            pass

        return output, image

    def _new_process(self, command: str, request_id: int) -> TProcess:
        process = TProcess(command, request_id)
        process.run()
        return process

    @check_permission
    @find_process_by_event
    async def _info_handler(self, event, process):
        await event.answer(str(process), alert=True)

    @check_permission
    @find_process_by_event
    async def _enter_handler(self, event, process):
        process.push("^m")
        await event.answer("Enter key pressed...")

    async def _new_watcher(self, chat_id, request_id, message):
        match = re.match(r"!watch\s(\d+)([s,m,h])\s(.+)", message)
        if not match:
            await self.bot.send_message(
                chat_id,
                "Wrong pattern, send /tasks to see valid examples",
                reply_to=request_id,
            )
            return

        self._watch_tasks[request_id] = message.replace("!watch", "")
        count, type_, file = match.groups()
        second_mapping = {
            "s": 1,
            "m": 60,
            "h": 60 * 60,
        }
        while True:
            if self._watch_tasks.get(request_id) is None:
                break
            await self.bot.send_file(chat_id, file, reply_to=request_id)
            delay = int(count) * second_mapping[type_]
            await asyncio.sleep(delay)

    async def _show_tasks(self, chat_id):
        message, buttons = self._get_tasks_message()
        await self.bot.send_message(
            chat_id, message, buttons=buttons, parse_mode="html"
        )

    def _get_tasks_message(self):
        from telethon import Button

        buttons = [
            [Button.inline(f"❌ {command}", f"cancell_task&{task_id}")]
            for task_id, command in self._watch_tasks.items()
        ]
        message = ACTIVE_TASKS_MSG
        if not buttons:
            buttons = None
            message = EMPTY_TASKS_MSG

        return message, buttons

    @check_permission
    async def _cancell_task_handler(self, event):
        task_id = int(event.data.decode().split("&")[-1])
        try:
            del self._watch_tasks[task_id]
        except KeyError:
            await event.answer("Dead task! create new one")
        message, buttons = self._get_tasks_message()
        await self.bot.edit_message(
            event.chat_id,
            message_id=event.message_id,
            message=message,
            buttons=buttons,
            parse_mode="html",
        )

    async def _upload_file(self, chat_id, path, request_id):
        if not os.path.isfile(path):
            await self.bot.send_message(
                chat_id, f"`{path}` is not a file", reply_to=request_id
            )
            return

        message = await self.bot.send_message(
            chat_id, "Uploading started...", reply_to=request_id
        )
        partial_callback = partial(
            self._progress_callback,
            chat_id=chat_id,
            message_id=message.id,
            title=f"Uploading `{path}`",
        )
        await self.bot.send_file(
            chat_id,
            file=path,
            reply_to=request_id,
            progress_callback=partial_callback,
        )

    async def _run_bot_father_commands(self, chat_id, command):
        if command.startswith("/image_on"):
            self._render = True
        elif command.startswith("/image_off"):
            self._render = False

        elif command.startswith("/tasks"):
            await self._show_tasks(chat_id)

        elif command.startswith("/interacive_mode"):
            if getattr(self._last_process, "is_running", None) is True:
                self.set_interactive_process(self._last_process)
            else:
                await self.bot.send_message(
                    chat_id,
                    "last process finished, you must select another process manually",
                )
        elif command.startswith("/normal_mode"):
            self.reset_interactive_process()

    async def _run_extra_commands(self, event, command, message):
        chat_id, request_id = event.chat_id, event.message.id

        if re.match(r"!get\s.+", message):
            path = message.split(" ", 1)[1]
            await self._upload_file(chat_id, path, request_id)

        elif command == "!setup_browser":
            await self.setup_browser()

        elif command == "!watch":
            asyncio.shield(self._new_watcher(chat_id, request_id, message))

        elif (
            command.startswith(("!trust", "!untrust"))
            and event.sender_id == self.admins[0]
        ):
            await self._trust_switcher(command, event, request_id)

    async def _trust_switcher(self, command, event, request_id):
        reply_message = await event.message.get_reply_message()
        reply_sender_id = reply_message.sender_id
        reply_to = request_id

        if command == "!trust":
            if reply_sender_id is None:
                message = "I can't trust to an Anonymous admin!"
            else:
                message = "Repeat your command please, you are a trusted user now"
                self.admins.append(reply_sender_id)
                reply_to = reply_message.id

        elif command == "!untrust" and reply_sender_id != self.admins[0]:
            message = "Done, removed from trusted users"
            try:
                self.admins.remove(reply_sender_id)
            except ValueError:
                pass

        await self.bot.send_message(event.chat_id, message, reply_to=reply_to)

    async def _send_download_buttons(self, event):
        from telethon import Button

        message_id, file_name = event.id, event.file.name
        buttons = []
        if os.path.exists(file_name):
            message = f"`{file_name}` currentlly exists on this directory"
            buttons.extend(
                [
                    [
                        Button.inline(
                            "Save as new file", data=f"savefile&new&{message_id}"
                        )
                    ],
                    [Button.inline("Overwrite", data=f"savefile&true&{message_id}")],
                ]
            )
        else:
            message = "Do you want to save this file on sever?"
            buttons.append([Button.inline("Yes", data=f"savefile&new&{message_id}")])

        buttons.append([Button.inline("Cancell", data="removeme")])

        await self.bot.send_message(
            event.chat_id,
            message,
            reply_to=message_id,
            buttons=buttons,
        )

    async def _change_directory(self, event):
        path = event.message.message.split(" ", 1)[-1]
        try:
            os.chdir(path)
        except Exception as error:
            await self.bot.send_message(
                event.chat_id,
                str(error),
                reply_to=event.message.id,
            )

    @check_permission
    async def _all_messages_handler(self, event):
        message = event.message.message
        escape = message.startswith("\\")
        chat_id, request_id = event.chat_id, event.message.id

        if event.file:
            await self._send_download_buttons(event)

        elif message.startswith(("cd", "/", "!")) and not escape:
            command = message.split(" ", 1)[0]
            if message.startswith("/"):
                await self._run_bot_father_commands(chat_id, command)
            elif message.startswith("!"):
                await self._run_extra_commands(event, command, message)
            else:
                await self._change_directory(event)

        elif self.interactive_process:
            message = message[1:] if escape else message
            self.interactive_process.push(message)
            # maybe background task finish sooner
            # also a minimum time must be passed from last update
            # editing a message for each input charachter not reasonable/possible
            next_update_arrived = (
                int(time()) - self.interactive_process.last_update_time >= 2
            )
            if self.interactive_process is not None and next_update_arrived:
                await self.response(self.interactive_process, chat_id)
        else:
            process = self._new_process(message, request_id)
            Telminal.all_processes[process.pid] = process

            asyncio.shield(self._run_in_background(process, chat_id))

    @check_permission
    @find_process_by_event
    async def _terminate_handler(self, event, process):
        process.terminate()

    @check_permission
    @find_process_by_event
    async def _html_handler(self, event, process):
        await self.bot.send_file(
            event.chat_id,
            process.html,
            reply_to=process.response_id,
        )

    def set_interactive_process(self, process):
        if isinstance(self.interactive_process, TProcess):
            self.interactive_process.is_interactive_process = False
        self.interactive_process = process
        process.is_interactive_process = True
        return f"You are talking to PID {process.pid}"

    def reset_interactive_process(self):
        if isinstance(self.interactive_process, TProcess):
            self.interactive_process.is_interactive_process = False
        self.interactive_process = None
        return "Normal mode activated"

    @check_permission
    @find_process_by_event
    async def _interactive_handler(self, event, process):
        if self.interactive_process is process:
            answer = self.reset_interactive_process()
        else:
            answer = self.set_interactive_process(process)

        await event.answer(answer, alert=True)
        await self.response(process, event.chat_id)

    @check_permission
    async def _inline_query_handler(self, event):
        command = "ls -la" if not event.text else f"ls -la | grep {event.text}"
        process = await asyncio.subprocess.create_subprocess_shell(
            command, stdin=PIPE, stdout=PIPE, stderr=PIPE
        )
        files = (await process.stdout.read()).decode().split("\n")

        builder = event.builder
        results = []

        file_name_pattern = re.compile(r"(.+)\s+\d{2}:*\d{2}\s+\d+\s")
        for file in files[: Telegram.INLINE_RESULT_LIMIT]:
            # `-` means this is a file and not a directory
            # `r` added for `-?????????` situations (permission)
            if file.startswith("-r"):
                file_name = file_name_pattern.findall(file[::-1])[0][::-1]
                results.append(
                    builder.article(
                        text=f"!get {file_name}", title=file_name, description=file
                    )
                )
        await event.answer(results=results, cache_time=0)

    async def _progress_callback(
        self, current, total, *, chat_id: int, message_id: int, title: str
    ):
        percent_str = f"{current / total:.2%}"
        percent_int = int(percent_str.split(".")[0])
        upload_finished = percent_int == 100

        emoji = "🟩"
        if upload_finished:
            emoji = "☑️"
            title = "Finished Successfully"

        emoji_count = 0 if percent_int <= 10 else int(percent_str[0])

        text = f"""\
        {title}
        {emoji_count * emoji} {percent_str}
        """
        # showing upload state to user each 5 second
        if (
            int(time() - self.all_progress_callback.get(message_id, 0)) > 5
            or upload_finished
        ):
            await self.bot.edit_message(chat_id, message_id=message_id, message=text)
            self.all_progress_callback[message_id] = time()

    @check_permission
    async def _confirm_download_handler(self, event):
        _, overwrite, message_id = event.data.decode().split("&")
        message = await self.bot.get_message(event.chat_id, int(message_id))
        partial_callback = partial(
            self._progress_callback,
            chat_id=event.chat_id,
            message_id=event.message_id,
            title="Downloading...",
        )

        file = message.file.name if overwrite == "true" else None
        await self.bot.download_media(
            message, progress_callback=partial_callback, file=file
        )

    @check_permission
    async def _cancell_download_handler(self, event):
        await event.delete()

    async def response(self, process: TProcess, chat_id: int):
        """Response to user and update process result message periodically"""
        media_output = process.media_output
        # update buttons must be once per response
        new_buttons = process.update_buttons()

        if not process.has_new_state(new_buttons, media_output):
            return

        output, image = await self.render_xtermjs(process)
        if not process.has_new_state(new_buttons, output):
            return

        if process.is_partial:
            if not any(output.split("\n")):
                # sometimes when process finished whit a kill signal
                # the final output is an empty line and in telegram
                # we can't send an empty message
                # but buttons update must apply to message
                output = None
            await self.bot.edit_message(
                chat_id,
                message=output,
                message_id=process.response_id,
                buttons=process.buttons,
                file=image,
            )
        else:
            process.response_id = (
                await self.bot.send_message(
                    chat_id,
                    output,
                    reply_to=process.request_id,
                    buttons=process.buttons,
                    file=image,
                )
            ).id
            self._last_process = process
            process.is_partial = True
        process.last_message = output

    async def _run_in_background(self, process: TProcess, chat_id: int):
        while process.is_running:
            partial_update_time = (process.run_time + 1) % 4 == 0
            try:
                if partial_update_time or process.response_id is None:
                    # first time fast response needed but next times
                    # for a partial update must passed at least 1 second
                    response_delay = 0.5 if process.response_id is None else 1.1
                    await asyncio.sleep(response_delay)
                    await self.response(process, chat_id)
            except Exception:
                # TODO should I reaction to this?
                pass
            finally:
                await asyncio.sleep(0.1)

        try:
            # maybe process will be finished before next update
            await self.response(process, chat_id)
        finally:
            if self.interactive_process is process:
                self.reset_interactive_process()

    def _exit_jobs(self):
        config = {
            "api_id": self._api_id,
            "api_hash": self._api_hash,
            "token": self._token,
            "admins": self.admins,
        }
        from telminal import PACKAGE_PATH

        with open(PACKAGE_PATH / "config.json", "w", encoding="utf-8") as file:
            file.write(json.dumps(config))

        temp_dir.cleanup()
