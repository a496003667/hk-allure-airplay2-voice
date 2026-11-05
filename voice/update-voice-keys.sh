#!/bin/zsh
# Update the voice assistant's API keys on the Allure.
#
#   ./update-voice-keys.sh
#
# Asks for the new keys (input is hidden; press Enter to keep a key as it
# is), saves them on this Mac (~/.baidu_speech_key, ~/.modelscope_key,
# mode 600), installs them on the speaker, and checks Baidu and ModelScope
# with the new keys.  If a check fails, the speaker's previous keys are put
# back.  Keys are never printed.
#
# Needs a way to reach the speaker's root shell.  Set these to your own tools:
#   HK_PUT   "<cmd> <local-file> <remote-path>"  uploads a file
#   HK_SHELL "<cmd> '<shell script>'"            runs a script as root
# The defaults are the author's mTLS maintenance-channel helpers (not part of
# this repository).
set -u
cd "${0:A:h}" || exit 1
HK_PUT=${HK_PUT:-"python3 hk_put.py"}
HK_SHELL=${HK_SHELL:-"python3 hk_secure.py --timeout 120"}

read -rs "BAK?百度 API Key（回车保持不变）: "; echo
read -rs "BSK?百度 Secret Key（回车保持不变）: "; echo
read -rs "MSK?魔搭 ModelScope Key（回车保持不变）: "; echo
BAK=${BAK//[[:space:]]/}; BSK=${BSK//[[:space:]]/}; MSK=${MSK//[[:space:]]/}

if [ -z "$BAK$BSK$MSK" ]; then
    echo "没有输入新的 key，什么都没改。"
    exit 0
fi
if { [ -n "$BAK" ] && [ -z "$BSK" ]; } || { [ -z "$BAK" ] && [ -n "$BSK" ]; }; then
    echo "百度的 API Key 和 Secret Key 要一起更新。"
    exit 1
fi

umask 077
tmp=$(mktemp -d) || exit 1
trap 'rm -rf "$tmp"' EXIT
: >"$tmp/keys"
[ -n "$BAK" ] && printf 'BAIDU_API_KEY=%s\nBAIDU_SECRET_KEY=%s\n' "$BAK" "$BSK" >>"$tmp/keys"
[ -n "$MSK" ] && printf 'MODELSCOPE_KEY=%s\n' "$MSK" >>"$tmp/keys"

echo "上传到音箱……"
${=HK_PUT} "$tmp/keys" /vendor/hk-voice/newkeys >/dev/null || { echo "上传失败"; exit 1; }

${=HK_SHELL} '
cd /vendor/hk-voice || exit 1
cp -p voice.conf voice.conf.bak-keys
for k in $(cut -d= -f1 newkeys); do sed -i "/^$k=/d" voice.conf; done
cat newkeys >>voice.conf
rm -f newkeys baidu.token
chmod 600 voice.conf voice.conf.bak-keys
if env LD_LIBRARY_PATH=/system/workdir/lib ./hk-voice check; then
    rm -f voice.conf.bak-keys
    echo "RESULT=ok"
else
    cp -p voice.conf.bak-keys voice.conf
    rm -f voice.conf.bak-keys baidu.token
    echo "RESULT=failed"
fi' | tee "$tmp/out" | grep -oE '(baidu token|baidu tts|modelscope): (ok|FAILED)'

if grep -q 'RESULT=ok' "$tmp/out"; then
    [ -n "$BAK" ] && printf 'BAIDU_API_KEY=%s\nBAIDU_SECRET_KEY=%s\n' "$BAK" "$BSK" >~/.baidu_speech_key
    [ -n "$MSK" ] && printf '%s\n' "$MSK" >~/.modelscope_key
    chmod 600 ~/.baidu_speech_key ~/.modelscope_key 2>/dev/null
    echo "新 key 已生效（音箱上的旧 key 已删除，Mac 上的副本也已更新）。"
else
    echo "检查没通过：音箱已恢复成原来的 key。请核对新 key 后再运行一次。"
    exit 1
fi
