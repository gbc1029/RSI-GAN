QUESTION_ID = "Grading ID"
GROUND_TRUTH_KEY = "Reward"
MODEL = "gpt-o4-mini-genai"

def format_input_dict(row):
    # Extract the inputs for the task from the row
    return {
        "domain": "imo_grading",
        "problem": row['Problem'],
        "solution": row['Solution'],
        "grading_guidelines": row['Grading guidelines'],
        "student_answer": row['Response'],
    }