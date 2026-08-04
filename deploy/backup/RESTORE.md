# Restore runbook

1. Остановите `api`, `worker` и `scheduler`; оставьте PostgreSQL запущенным.
2. Создайте отдельный чистый PostgreSQL-кластер или пустую базу. Не восстанавливайте поверх рабочей базы.
3. С теми же `RESTIC_*` и `AWS_*` переменными выполните `restic snapshots --tag kosto-vet` и выберите snapshot.
4. Выполните `restic restore <snapshot> --target /tmp/restore`, затем найдите `kosto-vet.dump`.
5. Восстановите: `pg_restore --clean --if-exists --no-owner --no-acl --dbname=<new_database> kosto-vet.dump`.
6. Укажите API временный `DATABASE_URL` новой базы, выполните `alembic current` и smoke-запросы `/health/ready`, каталог, staff login.
7. Переключите production только после проверки количества заказов, платежей, активных резервов и последних audit/outbox записей.

Restore регулярно проверяется в изолированной базе. Никогда не используйте production ResultURL Robokassa во время проверки.

