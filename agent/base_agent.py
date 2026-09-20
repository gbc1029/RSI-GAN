from abc import ABC, abstractmethod

from utils import trajectory_log


class AgentSystem(ABC):
    def __init__(
        self,
        model,
        chat_history_file='./outputs/trajectory.jsonl',
    ):
        self.model = model

        # Structured JSONL trajectory (one file per instance). The name is kept
        # as ``chat_history_file`` for backward compatibility.
        self.trajectory_file = chat_history_file
        trajectory_log.reset(chat_history_file)
        self.log = self._log

    def _log(self, message, level="INFO"):
        trajectory_log.append(self.trajectory_file,
                              {"kind": "log", "level": str(level), "text": str(message)})

    @abstractmethod
    def forward(self, *args, **kwargs):
        pass
