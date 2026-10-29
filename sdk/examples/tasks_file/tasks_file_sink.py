# SPDX-License-Identifier: Apache-2.0
"""tasks-file: a sink writing `task.create` intents as Obsidian Tasks lines (horae-sdk example).

The pipeline's `act` entry is `tasks-file.append`. The core routes each `task.create` intent
to `tasks-file.apply`; the sink appends `task_line_for(intent)` (the title escaped, threat
model T02) to `Tasks.md` in its single `paths.write` entry, as approved in `horae.lock`.
An example only: no deduplication, no locking, no undo.
"""

import os
from pathlib import Path

from horae_sdk import Applied, ApplyRequest, Paths, Permissions, Plugin, Sink, SinkError
from horae_sdk.tasks import task_line_for

FILE = "Tasks.md"


def _vault(paths: Paths) -> Path:
    if len(paths.write) != 1:
        raise SinkError("vault_unset", "tasks-file needs exactly one paths.write entry")
    return Path(paths.write[0])


class TasksFile(Sink):
    def identity(self, paths: Paths) -> str:
        return f"tasks-file:{os.path.realpath(_vault(paths))}"

    def apply(self, request: ApplyRequest) -> Applied:
        try:
            line = task_line_for(request.intent)
        except ValueError as error:
            raise SinkError("intent_invalid", str(error)) from None
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(_vault(request.paths) / FILE, flags, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return Applied(FILE, details={"line": line})


plugin = Plugin("tasks-file", permissions=Permissions(intents=["task.create"]))
plugin.add(TasksFile(["tasks-file.append"]))

if __name__ == "__main__":
    plugin.run()
