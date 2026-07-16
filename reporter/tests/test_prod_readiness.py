from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from queries import prod_item_readiness_sql


def test_prod_item_readiness_sql_uses_project_and_quarter_filters():
    sql = prod_item_readiness_sql(project_key="STORE", year=2026, quarter=2, limit=25)

    assert "v_prod_item_readiness" in sql
    assert "project_key = %s" in sql
    assert "EXTRACT(YEAR FROM quarter_start)::int = %s" in sql
    assert "EXTRACT(QUARTER FROM quarter_start)::int = %s" in sql
    assert "LIMIT %s" in sql
