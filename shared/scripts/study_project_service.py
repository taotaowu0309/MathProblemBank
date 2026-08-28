from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from shared.scripts.application_paths import APP_PATHS
from shared.scripts.latex_document_layout import DOCUMENT_LAYOUT_INPUT, sync_document_layout
from shared.scripts.latex_theorem_environments import sync_theorem_environments


ROOT = APP_PATHS.application_root
SUBJECTS_PATH = APP_PATHS.subjects_registry_path
WORKSPACE_ROOT = APP_PATHS.workspace_root


MATH_SUBJECTS: dict[str, dict[str, Any]] = {
    "数学分析": {
        "name": "数学分析",
        "folder_name": "MathAnalysis",
        "prefix": "MA",
        "domain": "math",
        "db": "MathAnalysis/data/math_analysis.db",
        "folder": "MathAnalysis",
        "backups": "MathAnalysis/backups",
        "exports": "MathAnalysis/exports",
        "chapters": "MathAnalysis/chapters",
        "inbox": "MathAnalysis/problems/inbox.tex",
        "pdf": "MathAnalysis/mathematical-analysis-problems.pdf",
        "pdf_filename": "mathematical-analysis-problems.pdf",
        "enabled": True,
    },
    "高等代数": {
        "name": "高等代数",
        "folder_name": "HigherAlgebra",
        "prefix": "HA",
        "domain": "math",
        "db": "HigherAlgebra/data/higher_algebra.db",
        "folder": "HigherAlgebra",
        "backups": "HigherAlgebra/backups",
        "exports": "HigherAlgebra/exports",
        "chapters": "HigherAlgebra/chapters",
        "inbox": "HigherAlgebra/problems/inbox.tex",
        "pdf": "HigherAlgebra/higher-algebra-problems.pdf",
        "pdf_filename": "higher-algebra-problems.pdf",
        "enabled": True,
    },
}

PHYSICS_SUBJECTS: dict[str, dict[str, Any]] = {
    "量子场论": {
        "name": "量子场论",
        "folder_name": "Physics/QuantumFieldTheory",
        "prefix": "QFT",
        "domain": "physics",
        "db": "Physics/QuantumFieldTheory/data/quantum_field_theory.db",
        "folder": "Physics/QuantumFieldTheory",
        "backups": "Physics/QuantumFieldTheory/backups",
        "exports": "Physics/QuantumFieldTheory/exports",
        "chapters": "Physics/QuantumFieldTheory/chapters",
        "inbox": "Physics/QuantumFieldTheory/problems/inbox.tex",
        "pdf": "Physics/QuantumFieldTheory/quantum-field-theory-problems.pdf",
        "pdf_filename": "quantum-field-theory-problems.pdf",
        "enabled": True,
    },
}

ENGLISH_SUBJECTS: dict[str, dict[str, Any]] = {
    "英语学习": {
        "name": "英语学习",
        "folder_name": "English",
        "prefix": "ENG",
        "domain": "english",
        "db": "English/data/english_learning.db",
        "folder": "English",
        "backups": "English/backups",
        "exports": "English/exports",
        "chapters": "English/lecture-notes/chapters",
        "inbox": "English/imports/inbox.tex",
        "pdf": "English/english-learning.pdf",
        "pdf_filename": "english-learning.pdf",
        "enabled": True,
    },
}

DEFAULT_SUBJECTS: dict[str, dict[str, Any]] = {
    **MATH_SUBJECTS,
    **PHYSICS_SUBJECTS,
    **ENGLISH_SUBJECTS,
}


COLLECTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS problem_collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    collection_type TEXT NOT NULL
        CHECK (collection_type IN ('personal', 'textbook', 'custom')),
    book_id INTEGER,
    description TEXT NOT NULL DEFAULT '',
    pdf_filename TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_problem_collections_type
ON problem_collections(collection_type);

CREATE INDEX IF NOT EXISTS idx_problem_collections_updated
ON problem_collections(updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS collection_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection_id INTEGER NOT NULL,
    canonical_problem_id INTEGER NOT NULL,
    item_order INTEGER NOT NULL DEFAULT 0,
    included INTEGER NOT NULL DEFAULT 1 CHECK (included IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(collection_id) REFERENCES problem_collections(id) ON DELETE CASCADE,
    FOREIGN KEY(canonical_problem_id) REFERENCES canonical_problems(id) ON DELETE CASCADE,
    UNIQUE(collection_id, canonical_problem_id)
);

CREATE INDEX IF NOT EXISTS idx_collection_items_collection
ON collection_items(collection_id, item_order, id);

CREATE INDEX IF NOT EXISTS idx_collection_items_included_order
ON collection_items(collection_id, included, item_order, id);

CREATE INDEX IF NOT EXISTS idx_collection_items_problem
ON collection_items(canonical_problem_id);

CREATE TABLE IF NOT EXISTS collection_books (
    collection_id INTEGER NOT NULL,
    book_id INTEGER NOT NULL,
    role TEXT NOT NULL DEFAULT 'main',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(collection_id, book_id),
    FOREIGN KEY(collection_id) REFERENCES problem_collections(id) ON DELETE CASCADE,
    FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_collection_books_book
ON collection_books(book_id);
"""


BOOK_EXTRA_COLUMNS = {
    "isbn": "TEXT NOT NULL DEFAULT ''",
    "cover_url": "TEXT NOT NULL DEFAULT ''",
    "external_url": "TEXT NOT NULL DEFAULT ''",
    "pdf_path": "TEXT NOT NULL DEFAULT ''",
}

CANONICAL_EXTRA_COLUMNS = {
    "solution_status": "TEXT CHECK (solution_status IS NULL OR solution_status IN ('Answered', 'Deferred', 'Open'))",
    "summary_tex": "TEXT NOT NULL DEFAULT ''",
}

CANONICAL_EXTRA_INDEXES = (
    """
    CREATE INDEX IF NOT EXISTS idx_canonical_problem_code
    ON canonical_problems(problem_code)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_canonical_solution_status_order
    ON canonical_problems(solution_status, chapter_code, section_code, problem_order, id)
    """,
)


def _abs(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else WORKSPACE_ROOT / path


def qid(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def table_columns(connection: sqlite3.Connection, name: str) -> list[str]:
    if not table_exists(connection, name):
        return []
    return [row[1] for row in connection.execute(f"PRAGMA table_info({qid(name)})")]


def integrity_check(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"数据库完整性检查失败：{result}")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise RuntimeError(f"数据库存在外键错误：{foreign_keys}")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent), newline="\n") as handle:
        handle.write(text)
        temp = Path(handle.name)
    os.replace(temp, path)


def subject_to_runtime(raw: dict[str, Any]) -> dict[str, Path]:
    return {
        "db": _abs(raw["db"]),
        "folder": _abs(raw["folder"]),
        "backups": _abs(raw["backups"]),
        "exports": _abs(raw["exports"]),
        "chapters": _abs(raw["chapters"]),
        "inbox": _abs(raw["inbox"]),
        "pdf": _abs(raw["pdf"]),
    }


def ensure_subject_registry() -> dict[str, dict[str, Any]]:
    defaults = MATH_SUBJECTS if APP_PATHS.public_release else DEFAULT_SUBJECTS
    if not SUBJECTS_PATH.exists():
        SUBJECTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(SUBJECTS_PATH, json.dumps(defaults, ensure_ascii=False, indent=2) + "\n")
    raw = json.loads(SUBJECTS_PATH.read_text(encoding="utf-8"))
    changed = False
    for name, default in defaults.items():
        if name not in raw:
            raw[name] = default
            changed = True
        else:
            for key, value in default.items():
                if key not in raw[name]:
                    raw[name][key] = value
                    changed = True
    if changed:
        atomic_write_text(SUBJECTS_PATH, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    return raw


def current_workspace() -> str:
    value = os.environ.get("STUDY_BANK_WORKSPACE", "math").strip().lower()
    return value if value in {"math", "physics", "english"} else "math"


def subject_domain(subject_name: str) -> str:
    raw = ensure_subject_registry().get(subject_name, {})
    domain = str(raw.get("domain") or "").strip().lower()
    if domain:
        return domain
    folder = str(raw.get("folder") or raw.get("folder_name") or "")
    return "physics" if folder.startswith("Physics/") or folder.startswith("Physics\\") else "math"


def load_subjects(workspace: str | None = None) -> dict[str, dict[str, Path]]:
    active_workspace = (workspace or current_workspace()).strip().lower()
    raw = ensure_subject_registry()
    return {
        name: subject_to_runtime(cfg)
        for name, cfg in raw.items()
        if cfg.get("enabled", True) and subject_domain(name) == active_workspace
    }


def subject_prefix(subject_name: str) -> str:
    raw = ensure_subject_registry().get(subject_name, {})
    prefix = str(raw.get("prefix") or "").strip()
    if prefix:
        return prefix
    return "MA" if subject_name == "数学分析" else "HA"


def physics_commands_tex() -> str:
    return r"""% Common theoretical-physics notation.

% Number systems and constants.
\providecommand{\R}{\mathbb{R}}
\providecommand{\C}{\mathbb{C}}
\providecommand{\Z}{\mathbb{Z}}
\providecommand{\N}{\mathbb{N}}
\providecommand{\Q}{\mathbb{Q}}
\providecommand{\ii}{\mathrm{i}}
\providecommand{\ee}{\mathrm{e}}
\providecommand{\dd}{\mathop{}\!\mathrm{d}}
\providecommand{\eps}{\varepsilon}
\providecommand{\hbarc}{\hbar}
\providecommand{\kb}{k_{\mathrm B}}

% General math wrappers.
\providecommand{\abs}[1]{\left|#1\right|}
\providecommand{\norm}[1]{\left\lVert#1\right\rVert}
\providecommand{\set}[1]{\left\{#1\right\}}
\providecommand{\paren}[1]{\left(#1\right)}
\providecommand{\bracket}[1]{\left[#1\right]}
\providecommand{\angles}[1]{\left\langle#1\right\rangle}
\providecommand{\comm}[2]{\left[#1,#2\right]}
\providecommand{\anticomm}[2]{\left\{#1,#2\right\}}

% Classical mechanics and field theory.
\providecommand{\vb}[1]{\boldsymbol{#1}}
\providecommand{\uv}[1]{\hat{\boldsymbol{#1}}}
\providecommand{\dv}[2]{\frac{\dd #1}{\dd #2}}
\providecommand{\pdv}[2]{\frac{\partial #1}{\partial #2}}
\providecommand{\grad}{\nabla}
\providecommand{\divg}{\nabla\!\cdot}
\providecommand{\curl}{\nabla\times}
\providecommand{\lag}{\mathcal{L}}
\providecommand{\ham}{\mathcal{H}}
\providecommand{\action}{S}

% Quantum mechanics.
\providecommand{\ket}[1]{\left|#1\right\rangle}
\providecommand{\bra}[1]{\left\langle#1\right|}
\providecommand{\braket}[2]{\left\langle#1\,\middle|\,#2\right\rangle}
\providecommand{\mel}[3]{\left\langle#1\,\middle|\,#2\,\middle|\,#3\right\rangle}
\providecommand{\op}[1]{\hat{#1}}
\providecommand{\Tr}{\operatorname{Tr}}

% Relativity and geometry.
\providecommand{\md}{\mu,\nu,\rho,\sigma}
\providecommand{\munu}{\mu\nu}
\providecommand{\metric}{g_{\mu\nu}}
\providecommand{\dAlembert}{\Box}
\providecommand{\lieD}{\mathcal{L}}
\providecommand{\covD}{\nabla}

% Statistical physics.
\providecommand{\partition}{Z}
\providecommand{\freeenergy}{F}
\providecommand{\avg}[1]{\left\langle #1 \right\rangle}

\DeclareMathOperator{\Arg}{Arg}
\DeclareMathOperator{\Log}{Log}
\DeclareMathOperator{\Res}{Res}
\DeclareMathOperator{\Rea}{Re}
\DeclareMathOperator{\Ima}{Im}
\DeclareMathOperator{\sgn}{sgn}

\numberwithin{equation}{section}
"""


def ensure_physics_preamble(folder: Path) -> None:
    preamble = folder / "preamble"
    preamble.mkdir(parents=True, exist_ok=True)
    math_preamble = ROOT / "MathAnalysis" / "preamble"
    if math_preamble.exists():
        shutil.copytree(math_preamble, preamble, dirs_exist_ok=True)
    (preamble / "commands.tex").write_text(physics_commands_tex(), encoding="utf-8")


def ensure_subject_storage(subject_name: str) -> None:
    raw = ensure_subject_registry()
    cfg = raw.get(subject_name)
    if not cfg:
        raise KeyError(subject_name)
    runtime = subject_to_runtime(cfg)
    for key in ("folder", "backups", "exports", "chapters"):
        runtime[key].mkdir(parents=True, exist_ok=True)
    (runtime["folder"] / "textbook").mkdir(parents=True, exist_ok=True)
    (runtime["folder"] / "figures").mkdir(exist_ok=True)
    runtime["db"].parent.mkdir(parents=True, exist_ok=True)
    runtime["inbox"].parent.mkdir(parents=True, exist_ok=True)
    if not runtime["inbox"].exists():
        runtime["inbox"].write_text("% 临时导入区\n", encoding="utf-8")
    domain = subject_domain(subject_name)
    if domain == "physics":
        ensure_physics_preamble(runtime["folder"])
    elif domain == "english":
        ensure_english_preamble(runtime["folder"])
    elif (ROOT / "MathAnalysis" / "preamble").exists() and not (runtime["folder"] / "preamble" / "packages.tex").exists():
        shutil.copytree(ROOT / "MathAnalysis" / "preamble", runtime["folder"] / "preamble", dirs_exist_ok=True)

    from shared.scripts.init_databases import SCHEMA

    with sqlite3.connect(runtime["db"]) as connection:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA foreign_keys=ON")
        prefix = str(cfg.get("prefix") or subject_prefix(subject_name)).strip()
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('subject', ?)", (subject_name,))
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('problem_prefix', ?)", (prefix,))
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('book_prefix', ?)", (f"{prefix}-B",))
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('domain', ?)", (subject_domain(subject_name),))
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', '1')")
        integrity_check(connection)
        connection.commit()
    ensure_learning_schema(runtime["db"], runtime["backups"])
    if domain == "english":
        from shared.scripts.english_learning_service import EnglishLearningService

        EnglishLearningService(runtime["folder"])
    main_name = (
        "english-learning.tex" if domain == "english"
        else f"{runtime['folder'].name.lower().replace('_', '-')}-problems.tex"
    )
    main_path = runtime["folder"] / main_name
    if not main_path.exists():
        if domain == "english":
            write_default_english_main_tex(main_path)
        else:
            write_default_main_tex(main_path, subject_name)
    with sqlite3.connect(runtime["db"]) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        if table_exists(connection, "problem_collections"):
            count = connection.execute("SELECT COUNT(*) FROM problem_collections").fetchone()[0]
            if int(count or 0) == 0:
                prefix = str(cfg.get("prefix") or subject_prefix(subject_name)).strip()
                code = next_code(connection, "problem_collections", "collection_code", f"{prefix}-C", 4)
                connection.execute(
                    """
                    INSERT INTO problem_collections(collection_code, name, collection_type, description, pdf_filename)
                    VALUES (?, ?, 'personal', ?, ?)
                    """,
                    (
                        code,
                        "英语网课讲义兼容项目" if domain == "english" else f"{subject_name}学习问题集",
                        "用于复用稳定网课录制基础设施；英语学习内容由独立领域表管理。"
                        if domain == "english" else "初学过程中遇到的问题。",
                        f"{code}.pdf",
                    ),
                )
                connection.commit()
            if domain == "english":
                row = connection.execute(
                    "SELECT collection_code FROM problem_collections ORDER BY id LIMIT 1"
                ).fetchone()
                if row is not None:
                    compatibility_dir = runtime["folder"] / "collections" / str(row[0])
                    compatibility_dir.mkdir(parents=True, exist_ok=True)
                    source_preamble = runtime["folder"] / "preamble"
                    if source_preamble.is_dir():
                        shutil.copytree(
                            source_preamble,
                            compatibility_dir / "preamble",
                            dirs_exist_ok=True,
                        )


def ensure_english_preamble(folder: Path) -> None:
    """Install a small English-first lecture preamble without math-bank semantics."""

    from shared.scripts.english_domain_profile import ENGLISH_LECTURE_SEMANTIC_PREAMBLE

    preamble = folder / "preamble"
    preamble.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        preamble / "packages.tex",
        "\\usepackage{fontspec}\n\\usepackage[most]{tcolorbox}\n"
        "\\usepackage{hyperref}\n\\usepackage{bookmark}\n\\usepackage{geometry}\n"
        "\\geometry{a4paper,margin=2.35cm}\n",
    )
    atomic_write_text(
        preamble / "english-learning-environments.tex",
        ENGLISH_LECTURE_SEMANTIC_PREAMBLE + "\n",
    )


def write_default_english_main_tex(path: Path) -> None:
    text = r"""\documentclass[UTF8,12pt,openany,oneside]{ctexbook}
\input{preamble/packages}
\input{preamble/english-learning-environments}
\hypersetup{pdftitle={English Learning},pdfauthor={English Learning Workspace}}
\begin{document}
\frontmatter
\begin{titlepage}
\centering
\vspace*{4cm}
{\Huge\bfseries English Learning\par}
\vspace{1cm}
{\large Grammar, Vocabulary, Reading, Writing, and Active Practice\par}
\vfill
{\large \today\par}
\end{titlepage}
\tableofcontents
\mainmatter
% BEGIN AUTO-GENERATED ENGLISH LECTURE INCLUDES
% END AUTO-GENERATED ENGLISH LECTURE INCLUDES
\end{document}
"""
    atomic_write_text(path, text)


def backup_database(db_path: Path, backup_dir: Path, reason: str) -> Path | None:
    if not db_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", reason).strip("_") or "migration"
    target = backup_dir / f"{db_path.stem}_before_{safe}_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S-%f')}.db"
    source = sqlite3.connect(db_path, timeout=30)
    dest = sqlite3.connect(target, timeout=30)
    try:
        source.execute("PRAGMA foreign_keys=ON")
        integrity_check(source)
        source.backup(dest)
        dest.commit()
    finally:
        dest.close()
        source.close()
    latest = backup_dir / f"{db_path.stem}_latest.db"
    try:
        shutil.copy2(target, latest)
    except OSError:
        pass
    return target


def ensure_learning_schema(db_path: Path, backup_dir: Path) -> Path | None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        existing_tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        needs_backup = bool(existing_tables) and (
            "problem_collections" not in existing_tables
            or "collection_items" not in existing_tables
            or "collection_books" not in existing_tables
            or any(column not in table_columns(connection, "books") for column in BOOK_EXTRA_COLUMNS)
            or any(column not in table_columns(connection, "canonical_problems") for column in CANONICAL_EXTRA_COLUMNS)
        )
    if needs_backup:
        backup = backup_database(db_path, backup_dir, "collections_schema")
    with sqlite3.connect(db_path, timeout=30) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(COLLECTION_SCHEMA)
        if table_exists(connection, "books"):
            columns = set(table_columns(connection, "books"))
            for column, definition in BOOK_EXTRA_COLUMNS.items():
                if column not in columns:
                    connection.execute(f"ALTER TABLE books ADD COLUMN {qid(column)} {definition}")
        if table_exists(connection, "canonical_problems"):
            columns = set(table_columns(connection, "canonical_problems"))
            for column, definition in CANONICAL_EXTRA_COLUMNS.items():
                if column not in columns:
                    connection.execute(f"ALTER TABLE canonical_problems ADD COLUMN {qid(column)} {definition}")
                    columns.add(column)
            if "solution_status" in columns:
                for statement in CANONICAL_EXTRA_INDEXES:
                    connection.execute(statement)
        connection.execute("PRAGMA optimize")
        integrity_check(connection)
        connection.commit()
    return backup


def next_code(connection: sqlite3.Connection, table: str, column: str, prefix: str, width: int) -> str:
    if not table_exists(connection, table):
        return f"{prefix}{1:0{width}d}"
    rows = connection.execute(
        f"SELECT {qid(column)} FROM {qid(table)} WHERE {qid(column)} LIKE ?",
        (f"{prefix}%",),
    ).fetchall()
    largest = 0
    pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    for row in rows:
        match = pattern.match(str(row[0] or "").strip())
        if match:
            largest = max(largest, int(match.group(1)))
    return f"{prefix}{largest + 1:0{width}d}"


def create_subject(subject_name: str, folder_name: str, prefix: str, domain: str | None = None) -> dict[str, Any]:
    subject_name = subject_name.strip()
    folder_name = re.sub(r"[^A-Za-z0-9_-]+", "", folder_name.strip())
    prefix = re.sub(r"[^A-Za-z0-9]+", "", prefix.strip().upper())
    domain = (domain or current_workspace()).strip().lower()
    if domain not in {"math", "physics"}:
        domain = "math"
    if not subject_name or not folder_name or not prefix:
        raise ValueError("学科名称、目录名和编号前缀都不能为空。")
    raw = ensure_subject_registry()
    if subject_name in raw:
        raise ValueError(f"学科已存在：{subject_name}")
    storage_folder = f"Physics/{folder_name}" if domain == "physics" else folder_name
    subject_dir = ROOT / storage_folder
    if subject_dir.exists() and any(subject_dir.iterdir()):
        raise ValueError(f"目录已存在且非空：{subject_dir}")
    db_name = f"{folder_name.lower().replace('-', '_')}.db"
    pdf_name = f"{folder_name.lower().replace('_', '-')}-problems.pdf"
    cfg = {
        "name": subject_name,
        "folder_name": storage_folder,
        "prefix": prefix,
        "domain": domain,
        "db": f"{storage_folder}/data/{db_name}",
        "folder": storage_folder,
        "backups": f"{storage_folder}/backups",
        "exports": f"{storage_folder}/exports",
        "chapters": f"{storage_folder}/chapters",
        "inbox": f"{storage_folder}/problems/inbox.tex",
        "pdf": f"{storage_folder}/{pdf_name}",
        "pdf_filename": pdf_name,
        "enabled": True,
    }
    runtime = subject_to_runtime(cfg)
    for key in ("folder", "backups", "exports", "chapters"):
        runtime[key].mkdir(parents=True, exist_ok=True)
    (runtime["folder"] / "textbook").mkdir(parents=True, exist_ok=True)
    runtime["db"].parent.mkdir(parents=True, exist_ok=True)
    runtime["inbox"].parent.mkdir(parents=True, exist_ok=True)
    runtime["inbox"].write_text("% 临时导入区\n", encoding="utf-8")
    if domain == "physics":
        ensure_physics_preamble(runtime["folder"])
    elif (ROOT / "MathAnalysis" / "preamble").exists():
        shutil.copytree(ROOT / "MathAnalysis" / "preamble", runtime["folder"] / "preamble", dirs_exist_ok=True)
    (runtime["folder"] / "figures").mkdir(exist_ok=True)
    sync_theorem_environments(runtime["folder"] / "preamble")
    sync_document_layout(runtime["folder"] / "preamble")
    from shared.scripts.init_databases import SCHEMA

    with sqlite3.connect(runtime["db"]) as connection:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('subject', ?)", (subject_name,))
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('problem_prefix', ?)", (prefix,))
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('book_prefix', ?)", (f"{prefix}-B",))
        connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES('domain', ?)", (domain,))
        integrity_check(connection)
        connection.commit()
    ensure_learning_schema(runtime["db"], runtime["backups"])
    write_default_main_tex(runtime["folder"] / f"{folder_name.lower().replace('_', '-')}-problems.tex", subject_name)
    raw[subject_name] = cfg
    atomic_write_text(SUBJECTS_PATH, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    with sqlite3.connect(runtime["db"]) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        code = next_code(connection, "problem_collections", "collection_code", f"{prefix}-C", 4)
        connection.execute(
            """
            INSERT INTO problem_collections(collection_code, name, collection_type, description, pdf_filename)
            VALUES (?, ?, 'personal', ?, ?)
            """,
            (code, f"{subject_name}学习问题集", "初学过程中遇到的问题。", f"{code}.pdf"),
        )
        connection.commit()
    return cfg


def write_default_main_tex(path: Path, title: str) -> None:
    text = rf"""\documentclass[UTF8,fontset=none,12pt,openany,oneside]{{ctexbook}}
\input{{preamble/packages}}
{DOCUMENT_LAYOUT_INPUT}
\input{{preamble/colors}}
\input{{preamble/commands}}
\input{{preamble/geometry}}
\input{{preamble/theorems}}
\input{{preamble/problem-bank-environments}}
\input{{preamble/chapter.title}}
\hypersetup{{pdftitle={{{title}问题集}}, pdfauthor={{Math Problem Bank}}, pdfsubject={{{title}}}}}
\begin{{document}}
\frontmatter
\begin{{titlepage}}
\centering
\vspace*{{4cm}}
{{\Huge\bfseries {title}问题集\par}}
\vspace{{1cm}}
{{\large 初学问题、教材习题与专题集合\par}}
\vfill
{{\large \today\par}}
\end{{titlepage}}
\tableofcontents
\mainmatter
% BEGIN AUTO-GENERATED CHAPTER INCLUDES
% END AUTO-GENERATED CHAPTER INCLUDES
\end{{document}}
"""
    atomic_write_text(path, text)


def console_python() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        normal = exe.with_name("python.exe")
        if normal.exists():
            return str(normal)
    return str(exe)
