# SFT Baselines using OpenAI

```bash
# Put these into .env file
OPENAI_API_KEY=...
```

Choose a domain: `search_arena` | `paper_review`, then:
```bash
# Format sft data
python -m baselines.sft_openai.format_data --domain <domain>

# Run sft
python -m baselines.sft_openai.run --domain <domain>
```

To cancel all jobs:
```bash
python -m baselines.sft_openai.cancel
```
