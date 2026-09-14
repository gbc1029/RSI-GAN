from openai import OpenAI
client = OpenAI()

after = None
while True:
    page = client.fine_tuning.jobs.list(limit=100, after=after)
    for job in page.data:
        if job.status in {"running","queued","validating_files"}:
            client.fine_tuning.jobs.cancel(job.id)
            print("Cancelled", job.id)
    if not page.has_more:
        break
    after = page.data[-1].id
