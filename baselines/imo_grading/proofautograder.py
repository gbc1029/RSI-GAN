# ProofAutorGrader from IMO-Bench https://arxiv.org/abs/2511.01846

import re

from agent.base_agent import AgentSystem
from agent.llm_withtools import chat_with_agent
from utils.common import extract_jsons


PROMPT = """You are an expert grader for the International Mathematics Olympiad (IMO).
Your task is to evaluate a proposed solution strictly and rigorously.
Keep in mind the standards at the IMO are extremely high: only arguments that are logically sound, complete, and precise should be rewarded.

### General Scoring Rubric
Scores are assigned on a 0-7 scale. The general guidelines are:
* **7 Points (Correct):** The solution is complete, correct, and fully rigorous. If the submission contains incorrect attempts or lines of reasoning but ultimately presents a complete and correct solution, it should still be awarded full points; the presence of earlier, discarded work does not detract from the final correct proof.
* **6 Points (Almost Correct):** The solution is almost correct with a sound core argument, but contains minor errors in calculation or small gaps in logic. Missing proofs for major components, unjustified claims, or sketchy arguments are **not** eligible for 6 points.
* **1 Point (Partial Progress):** The solution demonstrates substantial progress explicitly mentioned in the grading guidelines. Initial observations, reformulating the problem without making substantive headway, or proving partial results not mentioned in the grading guidelines are generally **not** eligible for this score.
* **0 Points (Incorrect):** The solution doesn't make substantial progress that is a key step in the full solution or is fundamentally flawed. All partial progress without key results or lacking rigor also fall in this category.

### Input Data and Interpretation
You are provided with the following:
1. **Problem Statement:** The IMO problem.
2. **Ground Truth Solution:** A reference solution. Assume this solution is correct. It demonstrates one valid approach.
3. **Specific Grading Guidelines:** Criteria for awarding credit for this specific problem. These guidelines take precedence over the General Scoring Rubric, especially for partial credit.
4. **Proposed Solution:** The student submission.

### Evaluation Process
You must follow this structured process:
1. **Analyze References:** Meticulously read and understand the problem and Ground Truth Solution check the Specific Grading Guidelines. Identify the key steps for a complete solution and the criteria for partial credit.
2. **Step-by-Step Verification:** Verify the logical validity and rigor of every step. Identify all flaws, gaps, assumptions, and errors. **Make sure you fully understand every piece of logic behind each step of the proposed solution, you must be careful for solutions that 'pretend' to be correct.**
3. **Assess Progress:** Determine the extent of non-trivial progress made.
4. **Score Determination:** Compare the findings against the Specific Grading Guidelines and the General Rubric to determine the final score.

### Output Requirements
You must provide your final score in the format <points>N out of 7</points>.
Ensure the '<points>' block is used **only once**, as your answer will be parsed based on the first <points> </points> block that appears in your whole response.

**PROBLEM STATEMENT**
{problem_statement}

**GROUND-TRUTH SOLUTION**
{solution}

**SPECIFIC GRADING GUIDELINES**
{grading_guidelines}

**PROPOSED SOLUTION**
{student_answer}

Present your detailed thought process and formal justification based on the scoring rubric and grading guidelines, and finally present your final score in the format below.

[Select one of the following options]
<points>7 out of 7</points>
<points>6 out of 7</points>
<points>1 out of 7</points>
<points>0 out of 7</points>
"""


class TaskAgent(AgentSystem):
    """
    An automatic grader for IMO-Proof Bench.
    """
    def forward(self, inputs):
        # Check if all required inputs are present
        if not all(key in inputs for key in ["problem", "solution", "grading_guidelines", "student_answer"]):
            return None, []

        # Get response
        instruction = PROMPT.format(
            problem_statement=inputs["problem"],
            solution=inputs["solution"],
            grading_guidelines=inputs["grading_guidelines"],
            student_answer=inputs["student_answer"],
        )
        new_msg_history = chat_with_agent(instruction, model=self.model, msg_history=[], logging=self.log)

        # Extract the response
        prediction = "None"
        try:
            raw_text = new_msg_history[-1].get('text', '')

            # Extract content between <points>...</points>
            match = re.search(r"<points>(.*?)</points>", raw_text, re.DOTALL)
            if match:
                points_text = match.group(1).strip()  # e.g., "7 out of 7"

                # Extract just the leading integer
                num_match = re.search(r"\d+", points_text)
                if num_match:
                    prediction = int(num_match.group())  # e.g., 7
                    # Map prediction to reward text
                    reward_map = {
                        0: "incorrect",
                        1: "partial",
                        6: "almost",
                        7: "correct",
                    }
                    prediction = reward_map.get(prediction, "None")
                else:
                    self.log("No numeric score found inside <points> tag.")
                    prediction = "None"

            else:
                self.log("No <points> tag found in model output.")
                prediction = "None"

        except Exception as e:
            self.log(f"Error extracting prediction: {e}")
            prediction = "None"

        return prediction, new_msg_history


if __name__ == "__main__":
    # Test the proofautograder
    grader = TaskAgent(model="gemini-2-5-pro")

    # Load dataset
    import pandas as pd
    csv_path = "./domains/imo/gradingbench.csv"
    df = pd.read_csv(csv_path)
    # Get the first data point
    row = df.iloc[0]
    print(row)

    # Run the grader
    from domains.imo.grading_utils import format_input_dict, GROUND_TRUTH_KEY
    input = format_input_dict(row)
    prediction, new_msg_history = grader.forward(input)
    print(f"Ground truth: {row[GROUND_TRUTH_KEY]}, prediction: {prediction}")
