name: Daily Opportunity Digest

on:
  schedule:
    # 7:00 AM IST = 1:30 AM UTC
    - cron: "30 1 * * *"
  workflow_dispatch: {}   # lets you manually trigger a run from the Actions tab to test

permissions:
  contents: write   # needed so the workflow can commit the updated digest to docs/

jobs:
  run-digest:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run digest script
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
          SMTP_USER: ${{ secrets.SMTP_USER }}
          SMTP_PASS: ${{ secrets.SMTP_PASS }}
          EMAIL_TO: ${{ secrets.EMAIL_TO }}
          EMAIL_FROM: ${{ secrets.EMAIL_FROM }}
          SMTP_HOST: ${{ secrets.SMTP_HOST }}
          SMTP_PORT: ${{ secrets.SMTP_PORT }}
        run: python main.py

      - name: Commit updated digest site
        run: |
          git config user.name "opportunity-digest-bot"
          git config user.email "actions@github.com"
          git add docs/
          git diff --cached --quiet || git commit -m "Update digest: $(date -u +'%Y-%m-%d')"
          git push
