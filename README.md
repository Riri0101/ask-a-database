# Text-to-SQL Agent

Ask a question in plain English. Gemini turns it into SQL.

## Setup

1. Create a virtual environment and install packages:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Put your Gemini key in `.env`:

```
GEMINI_API_KEY=your_real_key
```

3. Run the Streamlit app:

```bash
streamlit run app/streamlit_app.py
```

4. Run evaluation:

```bash
python -m evaluation.evaluate
```

## Layout

- `agent/` — Gemini text-to-SQL logic
- `evaluation/` — test questions and scoring
- `app/` — Streamlit UI
- `data/` — local databases or sample files (not committed)
