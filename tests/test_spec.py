import pytest

from zakup import spec


def test_spec_file_picks_technical_specification():
    lot = {"Files": [{"nameRu": "Проект договора", "filePath": "https://x/1"},
                     {"nameRu": "Техническая спецификация", "filePath": "https://x/2"}]}
    assert spec.spec_file(lot) == "https://x/2"
    assert spec.spec_file({"Files": None}) is None


def test_download_refuses_other_hosts():
    with pytest.raises(spec.SpecError):
        spec.download("https://evil.example/file.pdf")
    with pytest.raises(spec.SpecError):
        spec.download("http://ows.goszakup.gov.kz/download/x")


class _Page:
    def __init__(self, text):
        self.text = text

    def extract_text(self):
        return self.text


def test_extract_takes_russian_characteristics(monkeypatch):
    pages = [
        "Саны,\tкөлемі:\n4\nсипаттамасы: қазақша мәтін",
        "Количество:\n4\nЕдиница\tизмерения:\nШтука\nОписание и требуемые функциональные,\n"
        "технические, качественные и\nэксплуатационные характеристики\nзакупаемых товаров:\n"
        "\t\tСтолик\tжурнальный,   дерево\n\n Размеры: 101 см",
    ]
    monkeypatch.setattr(spec, "PdfReader", lambda _: type("R", (), {"pages": [_Page(p) for p in pages]}))
    assert spec.extract(b"%PDF") == "Столик журнальный, дерево\nРазмеры: 101 см"


def test_extract_reports_broken_pdf():
    with pytest.raises(spec.SpecError):
        spec.extract(b"not a pdf")


def test_isolated_parse_reports_errors_instead_of_dying():
    # Разбор — в отдельном процессе: битый или тяжёлый файл — ошибка лота, служба жива.
    with pytest.raises(spec.SpecError, match="не удалось прочитать файл"):
        spec.extract_isolated(b"%PDF-1.4 broken")


def test_attempt_is_counted_before_parsing(conn, monkeypatch):
    monkeypatch.setattr(spec, "download", lambda url: b"pdf")

    def dies(data):
        # Как если бы процесс разбора убили посреди работы: попытка уже записана.
        assert conn.execute("SELECT error FROM lot_specs WHERE lot_id = 1").fetchone() == ("разбирается",)
        raise spec.SpecError("файл слишком тяжёлый для разбора — откройте файл")
    monkeypatch.setattr(spec, "extract_isolated", dies)
    spec.fetch(conn, 1, "https://ows.goszakup.gov.kz/x")
    spec.fetch(conn, 1, "https://ows.goszakup.gov.kz/x")
    assert conn.execute("SELECT attempts, text FROM lot_specs WHERE lot_id = 1").fetchone() == (2, None)
    monkeypatch.setattr(spec, "extract_isolated", lambda data: ("Стол", None))
    spec.fetch(conn, 1, "https://ows.goszakup.gov.kz/x")
    assert conn.execute("SELECT attempts, text FROM lot_specs WHERE lot_id = 1").fetchone() == (0, "Стол")
