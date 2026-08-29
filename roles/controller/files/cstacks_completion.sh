# bash completion for cstacks (installed by roles/controller).
# Keep in sync with the case statement in cstacks.sh.
complete -W "run stop upgrade migrate bootstrap-db seed runner console container test database-backup logs tail-logs help" cstacks
