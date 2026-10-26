"""Create a local .env with a cryptographic DB password, without printing secrets."""
import secrets
from pathlib import Path


def main():
    destination = Path('.env')
    if destination.exists():
        print('.env уже существует; настройки сохранены.')
        return
    template = Path('.env.example').read_text(encoding='utf-8')
    password = secrets.token_hex(32)
    content = template.replace('change_this_before_running_8bD26X', password)
    with destination.open('x', encoding='utf-8') as output:
        output.write(content)
    try:
        destination.chmod(0o600)
    except OSError:
        pass  # Windows ACLs, rather than POSIX mode bits, control access.
    print('Создан .env со случайным паролем БД. Пароль не выводится в журнал.')


if __name__ == '__main__':
    main()
