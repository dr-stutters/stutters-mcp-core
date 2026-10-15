"""A secret loader used by test_secrets (MCP_SECRET_LOADERS=fake=tests.unit.loader_fixture:FakeLoader)."""


class FakeLoader:
    def load(self, ref: str) -> str:
        if ref == "missing":
            raise ValueError("no such secret 'missing'")
        return f"from-fake-{ref}"


class NotALoader:
    pass
