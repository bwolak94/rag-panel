"""RAG evaluation test suite.

Measures faithfulness, answer relevancy, context precision, and context recall
against a stored baseline. A regression of >5% on any metric blocks merge.

Run with:
    pytest tests/eval/ -m eval -v

Update baseline after intentional improvements:
    python tests/eval/update_baseline.py
"""
