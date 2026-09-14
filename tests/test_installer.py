from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _bash() -> str | None:
    """Locate a bash interpreter (CI/Linux/macOS/Git-Bash) or None."""
    return shutil.which("bash")


def _render(func_call: str) -> str:
    """Source scripts/install.sh and run a render function, returning stdout.

    Globals are assigned AFTER sourcing because install.sh re-initialises
    SERVICE_* / INSTALL_DIR to empty at load time.
    """
    script = (
        f"source '{INSTALL_SH.as_posix()}'\n"
        "SERVICE_USER=vels-bot; SERVICE_GROUP=vels-bot; SERVICE_HOME=/var/lib/vels-bot\n"
        "INSTALL_DIR=/opt/vels-claude; CFG_PROJECTS_DIR=/var/lib/vels-bot/projects\n"
        "SERVICE_NAME=vels-claude\n"
        f"{func_call}\n"
    )
    proc = subprocess.run(
        [_bash(), "-c", script],
        capture_output=True,
        # Скрипты содержат UTF-8 (русские комментарии). На Windows локальный
        # codec (cp1251) ронял декод — фиксируем utf-8 явно.
        encoding="utf-8",
        errors="replace",
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout



@unittest.skipIf(_bash() is None, "bash interpreter not available")
class ShellInstallerHardeningTests(unittest.TestCase):
    """Security-критичный рендеринг scripts/install.sh (H-5/H-7/L-2/L-13)."""

    def test_systemd_unit_keeps_code_read_only(self) -> None:
        # H-7: код/.venv (INSTALL_DIR) НЕ должны быть на запись у сервис-юзера.
        unit = _render("render_systemd_unit")
        rw_line = next(l for l in unit.splitlines() if l.startswith("ReadWritePaths="))
        ro_line = next(l for l in unit.splitlines() if l.startswith("ReadOnlyPaths="))
        # Запись разрешена только в data/, HOME и projects — но НЕ в корне кода.
        self.assertIn("/opt/vels-claude/data", rw_line)
        self.assertIn("/var/lib/vels-bot", rw_line)
        self.assertIn("/var/lib/vels-bot/projects", rw_line)
        rw_paths = rw_line.split("=", 1)[1].split()
        self.assertNotIn("/opt/vels-claude", rw_paths)  # сам код — не writable
        # Код помечен read-only явно.
        self.assertEqual(ro_line, "ReadOnlyPaths=/opt/vels-claude")

    def test_systemd_unit_runs_bot_when_token_is_set(self) -> None:
        unit = _render("CFG_TOKEN='123456:AAFabc'; render_systemd_unit")
        exec_line = next(l for l in unit.splitlines() if l.startswith("ExecStart="))
        self.assertEqual(exec_line, "ExecStart=/opt/vels-claude/.venv/bin/python -m src.main")

    def test_systemd_unit_runs_web_only_without_token(self) -> None:
        # Telegram теперь необязателен. `python -m src.main` без токена выходит с
        # кодом 1 → Restart=always загнал бы сервис в рестарт-петлю, поэтому
        # web-only установка должна запускаться через scripts/run_web.py.
        unit = _render("CFG_TOKEN=''; render_systemd_unit")
        exec_line = next(l for l in unit.splitlines() if l.startswith("ExecStart="))
        self.assertEqual(
            exec_line,
            "ExecStart=/opt/vels-claude/.venv/bin/python /opt/vels-claude/scripts/run_web.py",
        )
        self.assertIn("Description=AI-Panel (Web)", unit)

    def test_systemd_unit_retains_safe_sandbox_hardening(self) -> None:
        unit = _render("render_systemd_unit")
        for directive in (
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectKernelTunables=true",
            "ProtectKernelModules=true",
            "RestrictSUIDSGID=true",
            "LockPersonality=true",
        ):
            self.assertIn(directive, unit)

    def test_systemd_unit_omits_directives_that_break_startup(self) -> None:
        # Проверено вживую: эти директивы ломали старт (ProtectHome=true маскирует
        # /home → каталог проектов под ${SERVICE_HOME}/projects недоступен,
        # PermissionError). Не должны вернуться в юнит — H-7 держится на
        # ReadOnlyPaths + root-владении.
        unit = _render("render_systemd_unit")
        active = [
            l.strip() for l in unit.splitlines() if not l.strip().startswith("#")
        ]
        self.assertFalse(any(l.startswith("ProtectHome=") for l in active))
        self.assertFalse(any(l.startswith("ProtectSystem=strict") for l in active))
        self.assertFalse(any(l.startswith("RestrictAddressFamilies") for l in active))

    def test_caddy_domain_sets_hsts_and_csp_and_no_referrer(self) -> None:
        # H-5/L-2: TLS-блок несёт HSTS + строгий CSP + no-referrer.
        out = _render("render_caddyfile domain claude.example.com 8765")
        self.assertIn('Strict-Transport-Security "max-age=31536000; includeSubDomains"', out)
        self.assertIn('Referrer-Policy "no-referrer"', out)
        self.assertNotIn("no-referrer-when-downgrade", out)
        self.assertIn("Content-Security-Policy", out)
        self.assertIn("frame-ancestors 'none'", out)
        # Telegram-виджет на /login не должен сломаться.
        self.assertIn("https://telegram.org", out)
        self.assertIn("https://oauth.telegram.org", out)

    def test_caddy_plain_ip_has_no_hsts(self) -> None:
        # HSTS бессмысленен и не выставляется по http (IP-режим без TLS).
        out = _render("render_caddyfile ip '' 8765")
        self.assertNotIn("Strict-Transport-Security", out)
        # CSP/no-referrer всё равно присутствуют.
        self.assertIn("Content-Security-Policy", out)
        self.assertIn('Referrer-Policy "no-referrer"', out)

    def test_render_env_file_writes_anthropic_key(self) -> None:
        # Интерактивный вопрос про API-ключ в prompt_onboarding кладёт его в
        # CFG_ANTHROPIC_API_KEY → render_env_file должен записать его в .env,
        # иначе ключ не доедет до сервиса и Claude не авторизуется.
        out = _render("render_env_file tok 123 /proj '' '' '' '' sk-ant-TESTKEY")
        self.assertIn("ANTHROPIC_API_KEY=sk-ant-TESTKEY", out)

    def test_admin_password_has_strong_entropy(self) -> None:
        # L-13: >=18 байт энтропии (~24 base64url-символа), без спецсимволов .env.
        out = _render("gen_admin_password").strip()
        self.assertGreaterEqual(len(out), 22)
        self.assertRegex(out, r"^[A-Za-z0-9_-]+$")


@unittest.skipIf(_bash() is None, "bash interpreter not available")
class ShellScriptSyntaxTests(unittest.TestCase):
    """`bash -n` на всех правленых скриптах (синтаксис не сломан)."""

    SCRIPTS = (
        "install.sh",
        "update.sh",
        "platform-install.sh",
        "make-release.sh",
        "lib/python-env.sh",
    )

    def test_scripts_parse(self) -> None:
        for name in self.SCRIPTS:
            path = REPO_ROOT / "scripts" / name
            with self.subTest(script=name):
                proc = subprocess.run(
                    [_bash(), "-n", str(path)],
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)


class RequirementsLockTests(unittest.TestCase):
    """H-9: requirements.lock существует, запиннен и снабжён хэшами."""

    def test_lock_file_is_pinned_and_hashed(self) -> None:
        lock = REPO_ROOT / "requirements.lock"
        self.assertTrue(lock.exists(), "requirements.lock отсутствует")
        text = lock.read_text(encoding="utf-8")
        # Хэши обязательны для --require-hashes.
        self.assertIn("--hash=sha256:", text)
        # Каждая прямая зависимость должна быть запиннена через ==.
        for pkg in ("aiogram", "fastapi", "pydantic", "uvicorn", "PyJWT"):
            self.assertRegex(
                text,
                rf"(?im)^{pkg}==",
                f"{pkg} не запиннен через == в requirements.lock",
            )


class NoEmbeddedSecretTests(unittest.TestCase):
    """Раздаваемые скрипты НЕ должны содержать реальный GitHub-токен.

    Регресс: в публичном install.sh когда-то утёк зашитый fine-grained PAT —
    любой `curl` публичного файла получал доступ на чтение приватного репо.
    Этот тест навсегда блокирует возврат секрета в репозиторий. Плейсхолдеры
    вида `github_pat_xxx` (<20 символов после префикса) не считаются токеном.
    """

    DISTRIBUTED = ("install.sh", "platform-install.sh", "update.sh")
    TOKEN_RX = re.compile(r"github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}")

    def test_no_real_token_in_distributed_scripts(self) -> None:
        for name in self.DISTRIBUTED:
            text = (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")
            hits = self.TOKEN_RX.findall(text)
            self.assertEqual(hits, [], f"возможный реальный секрет в scripts/{name}: {hits}")


class PlatformBootstrapReleaseTests(unittest.TestCase):
    """Публичный бутстрап ставит из релиз-архива БЕЗ токена/клона приватного репо."""

    def _text(self) -> str:
        return (REPO_ROOT / "scripts" / "platform-install.sh").read_text(encoding="utf-8")

    def test_bootstrap_downloads_public_tarball(self) -> None:
        text = self._text()
        self.assertIn("RELEASE_URL", text)
        self.assertIn("tar", text)  # распаковка архива

    def test_bootstrap_verifies_checksum(self) -> None:
        self.assertIn("EXPECTED_SHA256", self._text())

    def test_bootstrap_has_no_token_or_private_clone(self) -> None:
        text = self._text()
        # Больше НЕ требует GH_TOKEN и НЕ тянет приватный репо через GitHub API.
        self.assertNotIn("oauth2:", text)
        self.assertNotIn("api.github.com/repos", text)
        self.assertNotIn("GH_TOKEN", text)

    def test_bootstrap_hardens_root_extraction(self) -> None:
        # Ревью [6]/[14]: tar от root распаковывает с --no-same-owner и отвергает
        # члены вне префикса vels-claude/ (defense-in-depth поверх sha-пина).
        text = self._text()
        self.assertIn("--no-same-owner", text)
        self.assertIn("vels-claude/", text)

    def test_bootstrap_pins_https_transport(self) -> None:
        # Ревью [12]: curl не должен молча даунгрейдиться на http по редиректу.
        self.assertIn("--proto", self._text())

    def test_bootstrap_rejects_stale_release_without_release_mode(self) -> None:
        # Ревью [3-3]: бутстрап проверяет, что распакованный install.sh реально
        # поддерживает RELEASE_SRC (защита от архива из старого install.sh).
        self.assertIn("без RELEASE_SRC", self._text())


@unittest.skipIf(_bash() is None, "bash interpreter not available")
class ReleaseTarballTests(unittest.TestCase):
    """scripts/make-release.sh строит секрет-free tar.gz через `git archive`."""

    def _run(self, ref: str = "HEAD") -> tuple[subprocess.CompletedProcess, str]:
        out = tempfile.mkdtemp()
        proc = subprocess.run(
            [_bash(), str(REPO_ROOT / "scripts" / "make-release.sh"), ref],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=REPO_ROOT,
            # ALLOW_DIRTY=1: тесты собирают из рабочего дерева, которое заведомо
            # «грязное» (незакоммиченные правки) — иначе dirty-guard прервёт сборку.
            env={**os.environ, "OUT_DIR": out, "ALLOW_DIRTY": "1"},
        )
        return proc, out

    def test_make_release_builds_tarball_and_prints_sha(self) -> None:
        proc, out = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SHA256=", proc.stdout)
        self.assertTrue(list(Path(out).glob("vels-claude-*.tar.gz")), "tar.gz не создан")

    def test_release_tarball_ships_code_but_no_secrets(self) -> None:
        proc, out = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        tarball = next(Path(out).glob("vels-claude-*.tar.gz"))
        # cd в каталог + basename: иначе на Windows Git Bash `tar` принимает
        # двоеточие в "C:/..." за remote-host и листинг выходит пустым.
        listing = subprocess.run(
            [_bash(), "-c", f"cd '{Path(out).as_posix()}' && tar -tzf '{tarball.name}'"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        ).stdout
        lines = listing.splitlines()
        # Код раздаётся.
        self.assertIn("vels-claude/src/main.py", lines)
        self.assertIn("vels-claude/scripts/install.sh", lines)
        # USER-GUIDE.md — рантайм-зависимость, а не документ для чтения: панель
        # «Документация» читает его с диска, и без него кнопка отдаёт 404
        # «guide not found». Остальной docs/** остаётся отсечённым.
        self.assertIn("vels-claude/docs/USER-GUIDE.md", lines)
        self.assertFalse(
            [
                l
                for l in lines
                if l.startswith("vels-claude/docs/") and not l.endswith("USER-GUIDE.md")
                and not l.endswith("/")
            ],
            "в архив попали внутренние доки помимо USER-GUIDE.md",
        )
        # Секреты/служебка — нет. `.env` точечно (а не `.env.example`).
        self.assertNotIn("vels-claude/.env", lines)
        self.assertFalse([l for l in lines if l.startswith("vels-claude/.git/")])
        self.assertFalse([l for l in lines if l.startswith("vels-claude/.venv/")])
        self.assertFalse([l for l in lines if l.startswith("vels-claude/data/")])

    def test_release_tarball_has_no_internal_docs_or_prod_secrets(self) -> None:
        # CRITICAL-регресс (ревью): git archive игнорирует .gitignore и без
        # .gitattributes export-ignore уносит ВСЕ tracked-доки, включая
        # docs/superpowers/HANDOFF-* с прод-IP, ssh-юзером и именем deploy-key.
        # Это было бы хуже утёкшего токена. Сканируем СОБРАННЫЙ архив.
        import tarfile

        proc, out = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        tarball = next(Path(out).glob("vels-claude-*.tar.gz"))
        ex = Path(tempfile.mkdtemp())
        with tarfile.open(tarball) as tf:
            names = tf.getnames()
            tf.extractall(ex, filter="data")
        # Внутренние доки не уехали.
        internal = [
            n for n in names
            if "HANDOFF" in n
            or "CODE_REVIEW" in n
            or n.rsplit("/", 1)[-1] == "CLAUDE.md"
            or "БЕЗОПАС" in n  # БЕЗОПАС…
            or "АУДИТ" in n  # АУДИТ…
        ]
        self.assertEqual(internal, [], f"внутренние доки в публичном архиве: {internal}")
        # Значения-секреты (не имена переменных!) не уехали ни в одном файле.
        markers = ("85.198.66.125", "id_ed25519_velsdeploy", "vlad@")
        leaked = []
        for p in ex.rglob("*"):
            if p.is_file():
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                for m in markers:
                    if m in text:
                        leaked.append((str(p.relative_to(ex)), m))
        self.assertEqual(leaked, [], f"прод-секреты в публичном архиве: {leaked}")


@unittest.skipIf(_bash() is None, "bash interpreter not available")
class InstallReleaseModeTests(unittest.TestCase):
    """install.sh: RELEASE_SRC копирует распакованный код БЕЗ git и токена."""

    def test_release_src_copies_code(self) -> None:
        src = Path(tempfile.mkdtemp()) / "vels-claude"
        (src / "src").mkdir(parents=True)
        (src / "src" / "main.py").write_text("x", encoding="utf-8")
        (src / "scripts").mkdir()
        (src / "scripts" / "install.sh").write_text("#", encoding="utf-8")
        dst = Path(tempfile.mkdtemp()) / "target"
        script = (
            f"source '{INSTALL_SH.as_posix()}'\n"
            f"RELEASE_SRC='{src.as_posix()}'\n"
            f"INSTALL_DIR='{dst.as_posix()}'\n"
            "install_or_update_repo\n"
        )
        proc = subprocess.run(
            [_bash(), "-c", script],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=REPO_ROOT,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((dst / "src" / "main.py").exists(), "код не скопирован")
        # Никакого git-репо в release-режиме.
        self.assertFalse((dst / ".git").exists())

    def _run_release(self, src: Path, dst: Path) -> subprocess.CompletedProcess:
        script = (
            f"source '{INSTALL_SH.as_posix()}'\n"
            f"RELEASE_SRC='{src.as_posix()}'\n"
            f"INSTALL_DIR='{dst.as_posix()}'\n"
            "install_or_update_repo\n"
        )
        return subprocess.run(
            [_bash(), "-c", script],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=REPO_ROOT,
        )

    def test_release_src_preserves_existing_env(self) -> None:
        # Повторная установка (обновление) НЕ затирает .env/data пользователя:
        # их нет в архиве, а prune их явно исключает.
        src = Path(tempfile.mkdtemp()) / "vels-claude"
        (src / "src").mkdir(parents=True)
        (src / "src" / "main.py").write_text("new", encoding="utf-8")
        dst = Path(tempfile.mkdtemp()) / "target"
        (dst / "src").mkdir(parents=True)  # существующая установка (есть src/main.py)
        (dst / "src" / "main.py").write_text("old", encoding="utf-8")
        (dst / ".env").write_text("TELEGRAM_BOT_TOKEN=mine", encoding="utf-8")
        (dst / "data").mkdir()
        (dst / "data" / "sessions.db").write_text("DB", encoding="utf-8")
        proc = self._run_release(src, dst)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual((dst / ".env").read_text(encoding="utf-8"), "TELEGRAM_BOT_TOKEN=mine")
        self.assertEqual((dst / "data" / "sessions.db").read_text(encoding="utf-8"), "DB")
        self.assertEqual((dst / "src" / "main.py").read_text(encoding="utf-8"), "new")

    def test_release_src_prunes_stale_files_and_git(self) -> None:
        # Ревью [2]/[13]: cp-overlay не удаляет файлы, удалённые между релизами,
        # и оставляет stale .git от старой git-установки (с протухшим токеном в
        # remote → update.sh уходит в неверную ветку). prune должен их убрать,
        # сохранив .env/data.
        src = Path(tempfile.mkdtemp()) / "vels-claude"
        (src / "src").mkdir(parents=True)
        (src / "src" / "main.py").write_text("new", encoding="utf-8")
        dst = Path(tempfile.mkdtemp()) / "target"
        (dst / "src").mkdir(parents=True)
        (dst / "src" / "main.py").write_text("old", encoding="utf-8")
        (dst / "src" / "ghost.py").write_text("stale", encoding="utf-8")  # удалён в новом релизе
        (dst / ".git").mkdir()
        (dst / ".git" / "config").write_text("[remote]", encoding="utf-8")  # stale git-установка
        (dst / ".env").write_text("SECRET", encoding="utf-8")
        (dst / "data").mkdir()
        (dst / "data" / "x.db").write_text("DB", encoding="utf-8")
        proc = self._run_release(src, dst)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((dst / "src" / "ghost.py").exists(), "ghost-файл не удалён")
        self.assertFalse((dst / ".git").exists(), "stale .git не удалён")
        self.assertEqual((dst / ".env").read_text(encoding="utf-8"), "SECRET")
        self.assertEqual((dst / "data" / "x.db").read_text(encoding="utf-8"), "DB")

    def test_release_src_keeps_unrelated_sibling_files(self) -> None:
        # Адверсариал-находка: prune не должен сносить посторонние файлы рядом
        # (backups/, заметки админа), которых нет в релиз-архиве — иначе молчаливая
        # потеря данных при каждом обновлении. Трогаем только релиз-управляемые пути.
        src = Path(tempfile.mkdtemp()) / "vels-claude"
        (src / "src").mkdir(parents=True)
        (src / "src" / "main.py").write_text("new", encoding="utf-8")
        dst = Path(tempfile.mkdtemp()) / "target"
        (dst / "src").mkdir(parents=True)
        (dst / "src" / "main.py").write_text("old", encoding="utf-8")
        (dst / "src" / "ghost.py").write_text("stale", encoding="utf-8")
        (dst / "backups").mkdir()
        (dst / "backups" / "dump.sql").write_text("BACKUP", encoding="utf-8")
        (dst / "admin-notes.txt").write_text("важное", encoding="utf-8")
        proc = self._run_release(src, dst)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Призрак внутри релиз-дерева убран…
        self.assertFalse((dst / "src" / "ghost.py").exists())
        # …а посторонние файлы рядом — целы.
        self.assertEqual((dst / "backups" / "dump.sql").read_text(encoding="utf-8"), "BACKUP")
        self.assertEqual((dst / "admin-notes.txt").read_text(encoding="utf-8"), "важное")

    def test_release_src_refuses_foreign_dir(self) -> None:
        # Ревью [7]: release-режим не должен затирать ЧУЖОЙ непустой каталог
        # (нет src/main.py = не наша установка) — как и git-путь (die).
        src = Path(tempfile.mkdtemp()) / "vels-claude"
        (src / "src").mkdir(parents=True)
        (src / "src" / "main.py").write_text("new", encoding="utf-8")
        dst = Path(tempfile.mkdtemp()) / "target"
        dst.mkdir()
        (dst / "someone-elses-app.txt").write_text("foreign", encoding="utf-8")
        proc = self._run_release(src, dst)
        self.assertNotEqual(proc.returncode, 0, "должен отказаться затирать чужой каталог")
        self.assertTrue((dst / "someone-elses-app.txt").exists(), "чужой файл удалён — недопустимо")


@unittest.skipIf(_bash() is None, "bash interpreter not available")
class UpdateReleaseInstallTests(unittest.TestCase):
    """update.sh на релиз-установке (нет .git) даёт понятную подсказку, не падает."""

    def test_update_without_git_points_to_reinstall(self) -> None:
        d = Path(tempfile.mkdtemp())
        (d / "src").mkdir()
        (d / "src" / "main.py").write_text("x", encoding="utf-8")
        (d / "requirements.txt").write_text("", encoding="utf-8")
        proc = subprocess.run(
            [_bash(), str(REPO_ROOT / "scripts" / "update.sh")],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "INSTALL_DIR": str(d)},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("релиз-архива", proc.stdout + proc.stderr)

    def test_update_autodetects_release_install_via_home_candidate(self) -> None:
        # Ревью [5]/[10]: авто-детект кандидатов требовал `.git`, поэтому
        # release-установка (без .git) в $HOME/vels-claude не находилась и
        # дружелюбное сообщение было недостижимо для дефолтного `curl|sudo bash`.
        home = Path(tempfile.mkdtemp())
        inst = home / "vels-claude"
        (inst / "src").mkdir(parents=True)
        (inst / "src" / "main.py").write_text("x", encoding="utf-8")
        (inst / "requirements.txt").write_text("", encoding="utf-8")
        # Standalone-копия: иначе BASH_SOURCE-ветка определит САМ репозиторий
        # (рядом со scripts/ есть ../src/main.py).
        standalone = Path(tempfile.mkdtemp()) / "update.sh"
        standalone.write_text(
            (REPO_ROOT / "scripts" / "update.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        neutral = tempfile.mkdtemp()  # нет ./src/main.py → cwd-ветка не сработает
        env = {**os.environ, "HOME": str(home)}
        env.pop("INSTALL_DIR", None)
        proc = subprocess.run(
            [_bash(), str(standalone)],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=neutral,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("релиз-архива", proc.stdout + proc.stderr)


@unittest.skipIf(_bash() is None, "bash interpreter not available")
class UpdateConnectionsKeyBackfillTests(unittest.TestCase):
    """update.sh идемпотентно бэкфиллит CONNECTIONS_SECRET_KEY (SP2) в .env.

    Без ключа src/apikeys/service.py возвращает None → хранение per-user
    Anthropic-ключей молча не работает (/api/apikey → 501). Прогоняем реальный
    update.sh на release-установке (нет .git → скрипт доходит до бэкфилла и
    выходит по дружелюбной подсказке, без git/сети).
    """

    def _release_install(self) -> Path:
        d = Path(tempfile.mkdtemp())
        (d / "src").mkdir()
        (d / "src" / "main.py").write_text("x", encoding="utf-8")
        (d / "requirements.txt").write_text("", encoding="utf-8")
        return d

    def _run_update(self, install_dir: Path) -> subprocess.CompletedProcess:
        env = {**os.environ, "INSTALL_DIR": str(install_dir)}
        return subprocess.run(
            [_bash(), str(REPO_ROOT / "scripts" / "update.sh")],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

    @staticmethod
    def _key(env_text: str) -> str | None:
        m = re.search(r"^CONNECTIONS_SECRET_KEY=(.+)$", env_text, re.M)
        return m.group(1) if m else None

    def test_backfill_adds_key_when_missing(self) -> None:
        d = self._release_install()
        (d / ".env").write_text("TELEGRAM_BOT_TOKEN=t\n", encoding="utf-8")
        proc = self._run_update(d)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        key = self._key((d / ".env").read_text(encoding="utf-8"))
        self.assertIsNotNone(key, "ключ не добавлен")
        import base64
        self.assertEqual(len(base64.urlsafe_b64decode(key)), 32)  # Fernet-совместим

    def test_backfill_preserves_existing_key(self) -> None:
        d = self._release_install()
        (d / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=t\nCONNECTIONS_SECRET_KEY=OLDKEEP\n", encoding="utf-8"
        )
        proc = self._run_update(d)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env_text = (d / ".env").read_text(encoding="utf-8")
        self.assertIn("CONNECTIONS_SECRET_KEY=OLDKEEP", env_text)
        # Ровно одна строка ключа — не задублировали и не ротировали.
        self.assertEqual(env_text.count("CONNECTIONS_SECRET_KEY="), 1)

    def test_backfill_is_idempotent_across_runs(self) -> None:
        d = self._release_install()
        (d / ".env").write_text("TELEGRAM_BOT_TOKEN=t\n", encoding="utf-8")
        self.assertEqual(self._run_update(d).returncode, 0)
        first = self._key((d / ".env").read_text(encoding="utf-8"))
        self.assertEqual(self._run_update(d).returncode, 0)
        second_text = (d / ".env").read_text(encoding="utf-8")
        self.assertEqual(self._key(second_text), first, "ключ ротировался при повторном запуске")
        self.assertEqual(second_text.count("CONNECTIONS_SECRET_KEY="), 1)

    def test_backfill_fills_empty_key_in_place_without_duplicate(self) -> None:
        # Косметический баг: пустая строка `CONNECTIONS_SECRET_KEY=` (есть, но без
        # значения) не матчила старый guard `=.` → бэкфилл ДОписывал вторую,
        # реальную строку, оставляя дубликат (пустая + рабочая). Теперь пустая
        # строка заполняется НА МЕСТЕ, без дубля.
        d = self._release_install()
        (d / ".env").write_text(
            "TELEGRAM_BOT_TOKEN=t\nCONNECTIONS_SECRET_KEY=\n", encoding="utf-8"
        )
        proc = self._run_update(d)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        env_text = (d / ".env").read_text(encoding="utf-8")
        # Ровно одна строка ключа — пустую заполнили на месте, дубликат не создали.
        self.assertEqual(env_text.count("CONNECTIONS_SECRET_KEY="), 1)
        key = self._key(env_text)
        self.assertIsNotNone(key, "пустой ключ не заполнен")
        import base64
        self.assertEqual(len(base64.urlsafe_b64decode(key)), 32)  # Fernet-совместим



PYTHON_ENV_LIB = REPO_ROOT / "scripts" / "lib" / "python-env.sh"


class PythonVersionSelectionStaticTests(unittest.TestCase):
    """scripts/lib/python-env.sh строит venv из интерпретатора >=3.11, а не из
    system python3 (H-2: общий sourceable helper для install.sh И update.sh —
    раньше эта логика жила только в install.sh, и update.sh никогда не
    пересоздавал legacy-venv на уже установленных ботах).

    Регресс: на Ubuntu 22.04 system python3=3.10 → `python3 -m venv` даёт venv на
    3.10, и pip падает на rpds-py==2026.5.1 ("No matching distribution found").
    Эти проверки статические (читают текст скрипта) — работают без Linux/apt.
    """

    def _sh(self) -> str:
        return INSTALL_SH.read_text(encoding="utf-8")

    def _lib(self) -> str:
        return PYTHON_ENV_LIB.read_text(encoding="utf-8")

    def test_install_sh_sources_shared_python_env_helper(self) -> None:
        # install.sh больше НЕ дублирует pick_python/ensure_python311 — источник
        # истины один файл, sourceable и update.sh-ом тоже.
        self.assertIn("lib/python-env.sh", self._sh())

    def test_update_sh_recreates_legacy_venv_before_pip(self) -> None:
        # H-2: update.sh проверяет версию venv ПЕРЕД pip-шагами и переиспользует
        # тот же общий helper вместо повторного кода.
        text = (REPO_ROOT / "scripts" / "update.sh").read_text(encoding="utf-8")
        self.assertIn("lib/python-env.sh", text)
        self.assertRegex(text, r"version_info\s*>=\s*\(3,\s*11\)")

    def test_defines_pick_python_with_version_guard(self) -> None:
        text = self._lib()
        self.assertIn("pick_python()", text)
        # Робастная проверка версии — ровно та, что требует таск.
        self.assertRegex(text, r"version_info\s*>=\s*\(3,\s*11\)")

    def test_pick_python_tries_explicit_minor_versions(self) -> None:
        text = self._lib()
        for cand in ("python3.13", "python3.12", "python3.11"):
            self.assertIn(cand, text, f"pick_python не перебирает {cand}")

    def test_venv_uses_selected_interpreter_not_system_python3(self) -> None:
        text = self._sh()
        # Больше НЕ хардкодим system python3 для venv…
        self.assertNotIn('python3 -m venv "$INSTALL_DIR/.venv"', text)
        # …а строим из выбранного pick_python/ensure_python311 интерпретатора.
        self.assertRegex(text, r'"\$pybin"\s+-m venv\s+"\$INSTALL_DIR/\.venv"')

    def test_fallback_installs_python_via_deadsnakes(self) -> None:
        # На старых Ubuntu (22.04) 3.11/3.12 нет в базовых репах — нужен PPA.
        text = self._lib()
        self.assertIn("deadsnakes", text)
        self.assertIn("python3.11-venv", text)

    def test_missing_python_dies_with_russian_hint(self) -> None:
        # Понятная финальная ошибка, если после всех попыток нет >=3.11.
        text = self._lib()
        self.assertRegex(text, r"Python ≥3\.11")


@unittest.skipIf(_bash() is None, "bash interpreter not available")
class PickPythonRuntimeTests(unittest.TestCase):
    """pick_python исполняется реально: если возвращает путь — он >=3.11."""

    def test_pick_python_returns_ge_311_or_reports_none(self) -> None:
        # Проверку версии выбранного интерпретатора делаем ВНУТРИ bash: путь из
        # `command -v` (напр. POSIX-путь в Git Bash) может быть неисполним для
        # Windows-subprocess напрямую, поэтому не выходим из bash-окружения.
        # Коды выхода: 0=найден и >=3.11, 3=не найден (skip), 4=найден, но <3.11.
        script = (
            f"source '{INSTALL_SH.as_posix()}'\n"
            'p="$(pick_python)" || exit 3\n'
            '"$p" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" || exit 4\n'
            'printf "%s" "$p"\n'
        )
        proc = subprocess.run(
            [_bash(), "-c", script],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=REPO_ROOT,
        )
        if proc.returncode == 3:
            self.skipTest("на хосте нет интерпретатора Python >=3.11 для pick_python")
        self.assertEqual(
            proc.returncode,
            0,
            f"pick_python выбрал интерпретатор < 3.11 или дал ошибку "
            f"(rc={proc.returncode}, out={proc.stdout!r}, err={proc.stderr!r})",
        )
        self.assertTrue(proc.stdout.strip(), "pick_python не напечатал путь")


if __name__ == "__main__":
    unittest.main()
