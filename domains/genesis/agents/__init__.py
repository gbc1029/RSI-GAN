import os
import sys

# Add the root directory to the Python path
sys.path.append(os.path.join(os.path.dirname(__file__), "../../.."))

from task_agent import TaskAgent
from agent.llm import CLAUDE_MODEL


class AgentFactory:
    """Factory class for creating agents based on configuration.

    The `AgentFactory` class is responsible for initializing the appropriate agent type
    based on the provided configuration, which includes setting up the LLM client and
    prompt builder.
    """

    def __init__(self, config):
        """Initialize the AgentFactory with configuration settings.

        Args:
            config (omegaconf.DictConfig): Configuration object containing settings for the agent and client.
        """
        self.config = config

    def create_agent(self, chat_history_file):
        """
        Create an agent instance with output to the chat history file.
        """
        return TaskAgent(
            model=CLAUDE_MODEL,
            chat_history_file=chat_history_file,
        )
