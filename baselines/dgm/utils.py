# Code adapted from https://github.com/jennyzzt/dgm/blob/main/prompts/self_improvement_prompt.py

import json
import os
import random
import pandas as pd
import importlib

from agent.llm import get_response_from_llm, CLAUDE_MODEL
from utils.common import extract_jsons, read_file
from utils.docker_utils import safe_log


CODING_AGENT_SUMMARY = """# Coding Agent Summary

- **Main File**: `coding_agent.py`
  - Primary Class: `CodingAgent`
  - The `forward()` function is the central entry point.
- **Tools**: `tools/`
  - The `tools/` directory contains various tools that LLMs can use to perform specific tasks.
  - Each tool must have a `tool_info()` function that returns a JSON object containing 'name', 'description', and 'input_schema'. The 'input_schema' should be a JSON object containing 'type', 'properties', and 'required'.
  - Each tool must have a `tool_function()` function that takes the arguments defined in input_schema, performs the tool's task, and returns a string.
  - See other tools for reference.
- **Utilities**: `utils/`
  - The `utils/` directory contains utility functions used across the codebase.

- **Additional Details**:
  - The agent is very good at automatically utilizing the right available tools at the right time. So do not have an agentic flow that explicitly forces a tool's usage.
  - Common tools, such as file editing and bash commands, are easy for the agent to recognize and use appropriately. However, more complex and niche tools may require explicit instructions in the prompt.
  - Tools should be designed to be as general as possible, ensuring they work across any GitHub repository. Avoid hardcoding repository-specific details or behaviors (e.g., paths).
  - Do not use 'while True' loops in the agent's code. This can cause the agent to get stuck and not respond.
  - Verify the implementation details of helper functions prior to usage to ensure proper integration and expected behavior.
  - Do not install additional packages or dependencies directly. Update `requirements.txt` if new dependencies are required and install them using `pip install -r requirements.txt`.
\n\n"""

CODING_AGENT_SUMMARY_CUSTOMIZED = """# Repository Summary

- **Coding Agent**: `coding_agent.py`
  - The `forward()` function is the central entry point.
  - The coding agent edits the code in this repository and self-modifies to improve its own coding ability.
- **Task Agent**: `task_agent.py`
  - The `forward()` function is the central entry point.
  - The task agent solves a given task. This agent produces the performance results.
- **Tools**: `tools/`
  - The `tools/` directory contains various tools that LLMs can use to perform specific tasks.
  - Each tool must have a `tool_info()` function that returns a JSON object containing 'name', 'description', and 'input_schema'. The 'input_schema' should be a JSON object containing 'type', 'properties', and 'required'.
  - Each tool must have a `tool_function()` function that takes the arguments defined in input_schema, performs the tool's task, and returns a string.
  - See other tools for reference.
- **Utilities**: `utils/`
  - The `utils/` directory contains utility functions used across the codebase.

- **Additional Details**:
  - The agent is very good at automatically utilizing the right available tools at the right time. So do not have an agentic flow that explicitly forces a tool's usage.
  - Common tools, such as file editing and bash commands, are easy for the agent to recognize and use appropriately. However, more complex and niche tools may require explicit instructions in the prompt.
  - Tools should be designed to be as general as possible, ensuring they work across any GitHub repository. Avoid hardcoding repository-specific details or behaviors (e.g., paths).
  - Do not use 'while True' loops in the agent's code. This can cause the agent to get stuck and not respond.
  - Verify the implementation details of helper functions prior to usage to ensure proper integration and expected behavior.
  - Do not install additional packages or dependencies directly. Update `requirements.txt` if new dependencies are required and install them using `pip install -r requirements.txt`.
\n\n"""

PROBLEM_DESCRIPTION_PROMPT = """# To Implement\n\n{implementation_suggestion}\n\n{problem_description}"""

DIAGNOSE_PROMPT = """
Here is the implementation of the coding agent.

# Coding Agent Implementation
----- Coding Agent Implementation Start -----
{code_codingagent}
----- Coding Agent Implementation End -----

Your task is to identify ONE detailed plan that would improve the coding agent. The improvement should not be specific to any particular GitHub issue or repository.

# Task Info
----- Task -----
{task_info}
----- Task End -----

# Report
----- Report -----
{report}
----- report End -----

# Agent Running Log
----- Agent Running Log Start -----
{md_log}
----- Agent Running Log End -----
Respond precisely in the following format including the JSON start and end markers:

<json>
...
</json>

In <json>, provide a JSON response with the following fields:
- "log_summarization": Analyze the above logs and summarize how the agent tried to solve the GitHub issue. Note which tools and how they are used, the agent's problem-solving approach, and any issues encountered.
- "potential_improvements": Identify potential improvements to the coding agent that could enhance its coding capabilities. Focus on the agent's general coding abilities (e.g., better or new tools usable across any repository) rather than issue-specific fixes (e.g., tools only usable in one framework). All necessary dependencies and environment setup have already been handled, so do not focus on these aspects.
- "improvement_proposal": Choose ONE high-impact improvement from the identified potential improvements and describe it in detail. This should be a focused and comprehensive plan to enhance the agent's overall coding ability.
- "implementation_suggestion": Referring to the coding agent's summary and implementation, think critically about what feature or tool could be added or improved to best implement the proposed improvement. If the proposed feature can be implemented by modifying the existing tools, describe the modifications needed, instead of suggesting a new tool.
- "problem_description": Phrase the improvement proposal and implementation suggestion as a GitHub issue description. It should clearly describe the feature so that a software engineer viewing the issue and the repository can implement it.

Your response will be automatically parsed, so ensure that the string response is precisely in the correct format."""

DIAGNOSE_PROMPT_CUSTOMIZED = """
Here is the implementation of the coding agent and task agent.

# Coding Agent Implementation
----- Coding Agent Implementation Start -----
{code_codingagent}
----- Coding Agent Implementation End -----

# Task Agent Implementation
----- Task Agent Implementation Start -----
{code_taskagent}
----- Task Agent Implementation End -----

Your task is to identify ONE detailed plan that would improve the coding/task agent. The improvement should not be specific to any particular task instance or repository.

# Task Info
----- Task -----
{task_info}
----- Task End -----

# Report
----- Report -----
{report}
----- report End -----

# Agent Running Log
----- Agent Running Log Start -----
{md_log}
----- Agent Running Log End -----

Respond precisely in the following format including the JSON start and end markers:

<json>
...
</json>

In <json>, provide a JSON response with the following fields:
- "log_summarization": Analyze the above logs and summarize how the agent tried to solve the given task. Note which tools and how they are used, the agent's problem-solving approach, and any issues encountered.
- "potential_improvements": Identify potential improvements to the coding/task agent that could enhance its coding/task-solving capabilities. Focus on the agent's general abilities (e.g., better or new tools usable across any repository) rather than issue-specific fixes (e.g., tools only usable in one framework). All necessary dependencies and environment setup have already been handled, so do not focus on these aspects.
- "improvement_proposal": Choose ONE high-impact improvement from the identified potential improvements and describe it in detail. This should be a focused and comprehensive plan to enhance the agent's overall coding/task-solving ability.
- "implementation_suggestion": Referring to the coding/task agent's summary and implementation, think critically about what feature or tool could be added or improved to best implement the proposed improvement. If the proposed feature can be implemented by modifying the existing tools, describe the modifications needed, instead of suggesting a new tool.
- "problem_description": Phrase the improvement proposal and implementation suggestion as a GitHub issue description. It should clearly describe the feature so that a software engineer viewing the issue and the repository can implement it.

Your response will be automatically parsed, so ensure that the string response is precisely in the correct format."""


def get_problem_description_prompt(response_json, customized=False):
    coding_agent_summary = CODING_AGENT_SUMMARY_CUSTOMIZED if customized else CODING_AGENT_SUMMARY
    return coding_agent_summary + PROBLEM_DESCRIPTION_PROMPT.format(
        implementation_suggestion=response_json["implementation_suggestion"],
        problem_description=response_json["problem_description"],
    )

def get_failed_entry_info(output_dir, gen_id, domain):
    eval_path = os.path.join(output_dir, f"gen_{gen_id}", f"{domain}_eval/")
    report_path = os.path.join(eval_path, "report.json")
    report = json.load(open(report_path, 'r'))

    if domain in ['search_arena', 'paper_review', 'imo_grading']:
        # Dynamically import functions based on the domain
        utils_prefix = domain.split("_", 1)[1] + "_" if domain.startswith("imo_") else ""
        domain_folder = domain.split('_')[0] if "imo_" in domain else domain
        utils_module_path = f"domains.{domain_folder}.{utils_prefix}utils"
        utils_module = importlib.import_module(utils_module_path)
        question_id_col = utils_module.QUESTION_ID

        # Get a random entry that failed
        question_ids_failed = report['question_ids_failed']
        entry_id = random.choice(question_ids_failed)

        # Get the task info for the entry
        predictions_path = os.path.join(eval_path, "predictions.csv")
        predictions = pd.read_csv(predictions_path)
        task_info = predictions[predictions[question_id_col] == entry_id].iloc[0].to_dict()

        # Get the chat history for the entry
        entry_chat_history_path = os.path.join(eval_path, f"agent_evals/chat_history_{entry_id}.md")
        md_log = read_file(entry_chat_history_path)

    elif 'balrog' in domain:
        # Get a random environment that did not get perfect score
        env_infos = report['environments']
        failed_envs = [env for env, info in env_infos.items() if info['progression_percentage'] < 100]
        failed_env = random.choice(failed_envs)

        # Get a random task in the environment that did not get perfect score
        env_report_path = os.path.join(eval_path, failed_env, f"{failed_env}_report.json")
        env_report = json.load(open(env_report_path, 'r'))
        task_infos = env_report['tasks']
        failed_tasks = [task for task, info in task_infos.items() if info['progression_percentage'] < 100]
        failed_task = random.choice(failed_tasks)

        # Get a random episode in the task that did not get perfect score
        task_folder = os.path.join(eval_path, failed_env, failed_task)
        json_files = [os.path.join(root, f)
                      for root, _, files in os.walk(task_folder)
                      for f in files if f.endswith('.json')]
        for json_file in json_files:
            episode_report = json.load(open(json_file, 'r'))
            if episode_report['progression'] < 1.0:
                episode_chat_history = json_file.replace('.json', '_chat_history.md')
                md_log = read_file(episode_chat_history)
                episode_csv = json_file.replace('.json', '.csv')
                task_info = pd.read_csv(episode_csv).to_dict()
                break

    elif domain == 'genesis_go2walking':
        # Get a random environment that did not get perfect score
        env_infos = report['environments']
        failed_envs = [env for env, info in env_infos.items() if info['average_fitness'] < 1.0]
        failed_env = random.choice(failed_envs)
        # Get a eval log
        eval_log_file = os.path.join(eval_path, "eval.log")
        md_log = read_file(eval_log_file)
        # Task info
        task_info = {
            "domain": "genesis_go2walking",
            "task_description": "Write a reward function that will guide the RL agent learn the following task:  Unitree Go2 robot dogs learn to walk forward according to the speed command.",
        }

    # Truncate to avoid exceeding LLM limits
    md_log = md_log[:250000]
    task_info = repr(task_info)[:100000]
    report = repr(report)[:100000]

    return task_info, md_log, report


def get_problem_statement(root_dir, output_dir, gen_id, domains, customized=False, max_attempts=3):
    # Get info for a failed entry
    domain = random.choice(domains)
    task_info, md_log, report = get_failed_entry_info(output_dir, gen_id, domain)

    # Use llm to diagnose
    diagnose_prompt = DIAGNOSE_PROMPT_CUSTOMIZED if customized else DIAGNOSE_PROMPT
    diagnose_prompt = diagnose_prompt.format(
        code_codingagent=read_file(os.path.join(root_dir, "./coding_agent.py")),
        code_taskagent=read_file(os.path.join(root_dir, "./task_agent.py")),
        task_info=task_info,
        md_log=md_log,
        report=report,
    )
    try:
        safe_log(f"Diagnose prompt: {repr(diagnose_prompt)}")
        response, _, _ = get_response_from_llm(msg=diagnose_prompt, model=CLAUDE_MODEL)
        safe_log(f"Diagnose response: {repr(response)}")
        response_json = extract_jsons(response)[-1]
        assert response_json, "empty response json"
        problem_statement = get_problem_description_prompt(response_json, customized=customized)

    except Exception as e:
        # Exception most probably due to not having json in the response
        safe_log(f"Error while diagnosing the problem: {e}")
        if max_attempts > 0:
            return get_problem_statement(output_dir, gen_id, domains, max_attempts=max_attempts-1)
        else:
            return None

    return problem_statement
