# Code adapted from https://github.com/lyang36/IMO25/blob/main/code/agent.py

from typing import Any, Dict, List, Optional, Tuple

from agent.base_agent import AgentSystem
from agent.llm_withtools import chat_with_agent


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

step1_prompt = """
### Core Instructions ###

*   **Rigor is Paramount:** Your primary goal is to produce a complete and rigorously justified solution. Every step in your solution must be logically sound and clearly explained. A correct final answer derived from flawed or incomplete reasoning is considered a failure.
*   **Honesty About Completeness:** If you cannot find a complete solution, you must **not** guess or create a solution that appears correct but contains hidden flaws or justification gaps. Instead, you should present only significant partial results that you can rigorously prove. A partial result is considered significant if it represents a substantial advancement toward a full solution. Examples include:
    *   Proving a key lemma.
    *   Fully resolving one or more cases within a logically sound case-based proof.
    *   Establishing a critical property of the mathematical objects in the problem.
    *   For an optimization problem, proving an upper or lower bound without proving that this bound is achievable.
*   **Use TeX for All Mathematics:** All mathematical variables, expressions, and relations must be enclosed in TeX delimiters (e.g., `Let $n$ be an integer.`).

### Output Format ###

Your response MUST be structured into the following sections, in this exact order.

**1. Summary**

Provide a concise overview of your findings. This section must contain two parts:

*   **a. Verdict:** State clearly whether you have found a complete solution or a partial solution.
    *   **For a complete solution:** State the final answer, e.g., "I have successfully solved the problem. The final answer is..."
    *   **For a partial solution:** State the main rigorous conclusion(s) you were able to prove, e.g., "I have not found a complete solution, but I have rigorously proven that..."
*   **b. Method Sketch:** Present a high-level, conceptual outline of your solution. This sketch should allow an expert to understand the logical flow of your argument without reading the full detail. It should include:
    *   A narrative of your overall strategy.
    *   The full and precise mathematical statements of any key lemmas or major intermediate results.
    *   If applicable, describe any key constructions or case splits that form the backbone of your argument.

**2. Detailed Solution**

Present the full, step-by-step mathematical proof. Each step must be logically justified and clearly explained. The level of detail should be sufficient for an expert to verify the correctness of your reasoning without needing to fill in any gaps. This section must contain ONLY the complete, rigorous proof, free of any internal commentary, alternative approaches, or failed attempts.

### Self-Correction Instruction ###

Before finalizing your output, carefully review your "Method Sketch" and "Detailed Solution" to ensure they are clean, rigorous, and strictly adhere to all instructions provided above. Verify that every statement contributes directly to the final, coherent mathematical argument.

"""

self_improvement_prompt = """
You have an opportunity to improve your solution. Please review your solution carefully. Correct errors and fill justification gaps if any. Your second round of output should strictly follow the instructions in the system prompt.
"""

check_verification_prompt = """
Can you carefully review each item in your list of findings? Are they valid or overly strict? An expert grader must be able to distinguish between a genuine flaw and a concise argument that is nonetheless sound, and to correct their own assessment when necessary.

If you feel that modifications to any item or its justification is necessary. Please produce a new list. In your final output, please directly start with **Summary** (no need to justify the new list).
"""

correction_prompt = """
Below is the bug report. If you agree with certain item in it, can you improve your solution so that it is complete and rigorous? Note that the evaluator who generates the bug report can misunderstand your solution and thus make mistakes. If you do not agree with certain item in the bug report, please add some detailed explanations to avoid such misunderstanding. Your new solution should strictly follow the instructions in the system prompt.
"""

verification_system_prompt = """
You are an expert mathematician and a meticulous grader for an International Mathematical Olympiad (IMO) level exam. Your primary task is to rigorously verify the provided mathematical solution. A solution is to be judged correct **only if every step is rigorously justified.** A solution that arrives at a correct final answer through flawed reasoning, educated guesses, or with gaps in its arguments must be flagged as incorrect or incomplete.

### Instructions ###

**1. Core Instructions**
*   Your sole task is to find and report all issues in the provided solution. You must act as a **verifier**, NOT a solver. **Do NOT attempt to correct the errors or fill the gaps you find.**
*   You must perform a **step-by-step** check of the entire solution. This analysis will be presented in a **Detailed Verification Log**, where you justify your assessment of each step: for correct steps, a brief justification suffices; for steps with errors or gaps, you must provide a detailed explanation.

**2. How to Handle Issues in the Solution**
When you identify an issue in a step, you MUST first classify it into one of the following two categories and then follow the specified procedure.

*   **a. Critical Error:**
    This is any error that breaks the logical chain of the proof. This includes both **logical fallacies** (e.g., claiming that `A>B, C>D` implies `A-C>B-D`) and **factual errors** (e.g., a calculation error like `2+3=6`).
    *   **Procedure:**
        *   Explain the specific error and state that it **invalidates the current line of reasoning**.
        *   Do NOT check any further steps that rely on this error.
        *   You MUST, however, scan the rest of the solution to identify and verify any fully independent parts. For example, if a proof is split into multiple cases, an error in one case does not prevent you from checking the other cases.

*   **b. Justification Gap:**
    This is for steps where the conclusion may be correct, but the provided argument is incomplete, hand-wavy, or lacks sufficient rigor.
    *   **Procedure:**
        *   Explain the gap in the justification.
        *   State that you will **assume the step's conclusion is true** for the sake of argument.
        *   Then, proceed to verify all subsequent steps to check if the remainder of the argument is sound.

**3. Output Format**
Your response MUST be structured into two main sections: a **Summary** followed by the **Detailed Verification Log**.

*   **a. Summary**
    This section MUST be at the very beginning of your response. It must contain two components:
    *   **Final Verdict**: A single, clear sentence declaring the overall validity of the solution. For example: "The solution is correct," "The solution contains a Critical Error and is therefore invalid," or "The solution's approach is viable but contains several Justification Gaps."
    *   **List of Findings**: A bulleted list that summarizes **every** issue you discovered. For each finding, you must provide:
        *   **Location:** A direct quote of the key phrase or equation where the issue occurs.
        *   **Issue:** A brief description of the problem and its classification (**Critical Error** or **Justification Gap**).

*   **b. Detailed Verification Log**
    Following the summary, provide the full, step-by-step verification log as defined in the Core Instructions. When you refer to a specific part of the solution, **quote the relevant text** to make your reference clear before providing your detailed analysis of that part.
"""

verification_reminder = """
### Verification Task Reminder ###

Your task is to act as an IMO grader. Now, generate the **summary** and the **step-by-step verification log** for the solution above. In your log, justify each correct step and explain in detail any errors or justification gaps you find, as specified in the instructions above.
"""


class TaskAgent(AgentSystem):
    """
    Agent that implements the original IMO solver logic using the AgentSystem
    + chat_with_agent abstraction.

    Inputs expected in forward():
        inputs["problem"]: str  (required) - the problem statement
        inputs["other_prompts"]: List[str] (optional)
        inputs["max_iterations"]: int (optional, default 30)
        inputs["required_correct_passes"]: int (optional, default 5)
        inputs["max_error_passes"]: int (optional, default 10)

    Returns:
        prediction: str (the final solution text)
        msg_history: List[Dict[str, Any]] (message history from the *last*
                     solver call, not the verifier calls)
    """

    # ------------------------------------------------------------------
    # Low-level helper for calling the LLM through chat_with_agent
    # ------------------------------------------------------------------
    def _call_llm(
        self,
        instruction: str,
        msg_history: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        if msg_history is None:
            msg_history = []

        new_msg_history = chat_with_agent(
            instruction,
            model=self.model,
            msg_history=msg_history,
            logging=self.log,
            tools_available=[],
        )

        # Extract last message text
        try:
            raw_text = new_msg_history[-1].get("text", "")
        except Exception as e:
            self.log(f"Error extracting LLM output: {e}")
            raw_text = ""

        return raw_text, new_msg_history

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_detailed_solution(
        solution: str,
        marker: str = "Detailed Solution",
        after: bool = True,
    ) -> str:
        """
        Extracts text before/after the given marker from the solution string.
        """
        idx = solution.find(marker)
        if idx == -1:
            return ""
        if after:
            return solution[idx + len(marker) :].strip()
        else:
            return solution[:idx].strip()

    def _verify_solution(
        self,
        problem_statement: str,
        solution: str,
    ) -> Tuple[str, str]:
        """
        Verification loop, using chat_with_agent twice:
        1) For detailed verification (grader-style).
        2) For a yes/no check on whether grader thinks it is correct.
        Returns:
            bug_report: str
            yesno_verdict: str (raw "yes"/"no" or text containing that)
        """

        dsol = self._extract_detailed_solution(solution)

        # Build combined text for verifier
        verification_input = f"""
======================================================================
### Problem ###

{problem_statement}

======================================================================
### Solution ###

{dsol}

{verification_reminder}
"""

        # First call: grader verification
        full_verifier_instruction = (
            verification_system_prompt + "\n\n" + verification_input
        )
        self.log(">>>>>>> Start verification.")
        verification_output, _ = self._call_llm(full_verifier_instruction)

        # Second call: yes/no meta-check
        check_correctness = (
            'Response in "yes" or "no". '
            "Is the following statement saying the solution is correct, "
            "or does not contain critical error or a major justification gap?\n\n"
            + verification_output
        )
        self.log(">>>>>>> Is verification good?")
        yesno_output, _ = self._call_llm(check_correctness)

        bug_report = ""

        # If verifier is NOT happy, extract the bug report (summary part)
        if "yes" not in yesno_output.lower():
            bug_report = self._extract_detailed_solution(
                verification_output,
                marker="Detailed Verification",
                after=False,
            )
            self.log(">>>>>>> Bug report:")
            self.log(repr(bug_report))

        return bug_report, yesno_output

    def _init_exploration(
        self,
        problem_statement: str,
        other_prompts: Optional[List[str]] = None,
    ) -> Tuple[str, str, str, str, List[Dict[str, Any]]]:
        """
        Initial exploration:
          1. Call with step1_prompt + problem to get first solution.
          2. Send self_improvement_prompt as follow-up user message
             to refine the solution.
          3. Verify the refined solution.

        Returns:
            initial_output: str      - first solution (pre self-improvement)
            solution: str            - improved solution
            bug_report: str          - initial bug report from verifier (may be empty)
            yesno_verdict: str       - raw yes/no-style string
            last_history: List[dict] - last msg_history from solver calls
        """
        if other_prompts is None:
            other_prompts = []

        # Build the initial instruction: system-like part + problem + extra hints
        base_instruction = step1_prompt + "\n\n### Problem ###\n\n" + problem_statement
        if other_prompts:
            extra = "\n\n".join(other_prompts)
            base_instruction += "\n\n### Additional Instructions ###\n" + extra

        # 1) Initial solution
        self.log(">>>>>>> Getting first solution:")
        initial_output, msg_history = self._call_llm(base_instruction)

        # 2) Self improvement in same conversation
        self.log(">>>>>>> Self improvement start:")
        improved_output, msg_history = self._call_llm(
            self_improvement_prompt, msg_history=msg_history
        )

        # 3) Verify
        self.log(">>>>>>> Verify the solution.")
        bug_report, yesno_verdict = self._verify_solution(
            problem_statement, improved_output
        )

        return initial_output, improved_output, bug_report, yesno_verdict, msg_history

    def _refine_solution(
        self,
        problem_statement: str,
        current_solution: str,
        bug_report: str,
        other_prompts: Optional[List[str]] = None,
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Take the current solution and bug report, and ask the model
        to fix / refine the solution accordingly.
        """

        if other_prompts is None:
            other_prompts = []

        # We rebuild a fresh instruction that includes:
        # - the main step1_prompt (acts as "system")
        # - the problem
        # - the previous solution
        # - the bug report and correction_prompt
        base = [step1_prompt, "### Problem ###", problem_statement]

        if other_prompts:
            base.append("### Additional Instructions ###")
            base.extend(other_prompts)

        base.append("### Previous Solution ###")
        base.append(current_solution)

        if bug_report.strip():
            base.append("### Bug Report from Verifier ###")
            base.append(bug_report)

        base.append(correction_prompt)

        instruction = "\n\n".join(base)

        new_solution, msg_history = self._call_llm(instruction)

        return new_solution, msg_history

    # ------------------------------------------------------------------
    # Main public API: forward()
    # ------------------------------------------------------------------
    def forward(
        self,
        inputs: Dict[str, Any],
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        High-level solver interface, analogous to the original `agent(...)`
        function but in AgentSystem-style.

        Args:
            inputs["problem"]: str (required)
            inputs["other_prompts"]: list[str] (optional)
            inputs["max_iterations"]: int (optional, default 30)
            inputs["required_correct_passes"]: int (optional, default 5)
            inputs["max_error_passes"]: int (optional, default 10)

        Returns:
            prediction: str
            msg_history: List[dict] from the last solver call
        """
        problem_statement: str = inputs["problem"]
        other_prompts: List[str] = []
        max_iterations: int = 30
        required_correct_passes: int = 5
        max_error_passes: int = 10

        # Initial exploration (one loop of solve + self-improve + verify)
        (
            _initial_output,
            solution,
            bug_report,
            yesno_verdict,
            last_history,
        ) = self._init_exploration(problem_statement, other_prompts)

        error_count = 0
        correct_count = 1 if "yes" in yesno_verdict.lower() else 0

        # Main refinement loop
        for iteration in range(1, max_iterations):
            self.log(
                f"Number of iterations: {iteration}, "
                f"number of correct passes: {correct_count}, "
                f"number of errors: {error_count}"
            )

            # If last verification was not good, refine
            if "yes" not in yesno_verdict.lower():
                correct_count = 0
                error_count += 1
                self.log(">>>>>>> Verification does not pass, correcting ...")
                solution, last_history = self._refine_solution(
                    problem_statement,
                    current_solution=solution,
                    bug_report=bug_report,
                    other_prompts=other_prompts,
                )

            # Verify again
            self.log(">>>>>>> Verify the solution.")
            bug_report, yesno_verdict = self._verify_solution(
                problem_statement, solution
            )

            if "yes" in yesno_verdict.lower():
                self.log(">>>>>>> Solution is good, verifying again ...")
                correct_count += 1
                error_count = 0
            else:
                correct_count = 0
                error_count += 1

            # Stopping criteria
            if correct_count >= required_correct_passes:
                self.log(">>>>>>> Correct solution found.")
                self.log(repr(solution))
                break

            if error_count >= max_error_passes:
                self.log(">>>>>>> Failed in finding a correct solution.")
                break

        # Return final solution text and the last solver msg_history
        prediction = solution
        return prediction, last_history


if __name__ == "__main__":
    # Example usage
    agent = TaskAgent(model="gpt-4o-mini-genai")

    # Example: load from CSV as in your skeleton
    import pandas as pd

    csv_path = "./domains/imo/proofbench.csv"
    df = pd.read_csv(csv_path)

    # Get the first data point
    row = df.iloc[0]
    print(row)

    from domains.imo.proof_utils import format_input_dict, GROUND_TRUTH_KEY

    input_dict = format_input_dict(row)
    prediction, new_msg_history = agent.forward(input_dict)
    print(f"Prediction: {repr(prediction[:500])}...")
