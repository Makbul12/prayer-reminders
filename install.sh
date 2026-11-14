#!/data/data/com.termux/files/usr/bin/bash
# Prayer reminders installer for Termux. Run with:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Makbul12/prayer-reminders/main/install.sh)"
set -e
REPO="https://github.com/Makbul12/prayer-reminders.git"
APP="$HOME/prayer"
export DEBIAN_FRONTEND=noninteractive SKIP_CYTHON=1

echo ">> Installing (5-10 minutes, please wait)..."
pkg update -y -o Dpkg::Options::="--force-confnew"
pkg install -y -o Dpkg::Options::="--force-confnew" python git
pip install -q --upgrade pychromecast gTTS

if [ -d "$APP/.git" ]; then git -C "$APP" pull --ff-only -q; else git clone -q "$REPO" "$APP"; fi

mkdir -p "$HOME/.termux/boot"
cat > "$HOME/.termux/boot/start-prayer-reminders" <<'BOOT'
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd "$HOME/prayer" && git pull --ff-only -q
exec python prayer_reminders.py run >> "$HOME/prayer/reminders.log" 2>&1
BOOT
chmod +x "$HOME/.termux/boot/start-prayer-reminders"

echo ">> Your speakers:"
python "$APP/prayer_reminders.py" find
echo ">> Testing (listen to your Google speaker)..."
python "$APP/prayer_reminders.py" test || true

pkill -f "[p]rayer_reminders.py run" 2>/dev/null || true
nohup "$HOME/.termux/boot/start-prayer-reminders" >/dev/null 2>&1 &
echo
python "$APP/prayer_reminders.py" today
echo
echo ">> DONE. Reminders are running. You can close Termux (don't force-stop it)."
