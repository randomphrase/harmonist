# Login shells reset PATH before reading /etc/profile.d.
# Keep the project environment available to TRAMP and shell logins.
export VIRTUAL_ENV=/opt/venv
case ":$PATH:" in
    *:/opt/venv/bin:*) ;;
    *) export PATH="/opt/venv/bin:$PATH" ;;
esac
