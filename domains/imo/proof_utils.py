QUESTION_ID = "Problem ID"
GROUND_TRUTH_KEY = "Solution"
MODEL = "gpt-o4-mini-genai"

def format_input_dict(row):
    # Extract the inputs for the task from the row
    return {
        "domain": "imo_proof",
        "problem": row['Problem'],
    }