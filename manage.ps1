param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("setup", "migrate", "upgrade", "run", "worker", "test", "check", "css")]
    [string]$Command
)

$venvPython = ".venv\Scripts\python.exe"

switch ($Command) {
    "setup" {
        python -m venv .venv
        & $venvPython -m pip install --upgrade pip
        & $venvPython -m pip install -r requirements-dev.txt
    }
    "migrate" {
        & $venvPython -m flask db migrate
    }
    "upgrade" {
        & $venvPython -m flask db upgrade
    }
    "run" {
        & $venvPython -m flask run
    }
    "worker" {
        & $venvPython -m celery -A celery_worker.celery worker --pool=solo --loglevel=info
    }
    "test" {
        & $venvPython -m pytest
    }
    "check" {
        # Vor jedem Commit: Syntax-/Importfehler, ungenutzte Importe, volle Testsuite.
        & $venvPython -m ruff check .
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $venvPython -m pytest
    }
    "css" {
        # Tailwind neu kompilieren, nachdem Templates/tailwind_source.css geaendert wurden.
        # tools/tailwindcss.exe ist die eigenstaendige CLI-Binary (kein Node.js noetig,
        # siehe README) - lokal per Download-Befehl aus dem README zu beschaffen, gitignored.
        & "tools\tailwindcss.exe" -i "app\static\css\tailwind_source.css" -o "app\static\css\app.css" --minify
    }
}
