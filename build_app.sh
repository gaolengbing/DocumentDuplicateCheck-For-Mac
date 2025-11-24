#!/bin/bash
set -e  # 遇到错误立即退出，避免继续执行
set -u  # 遇到未定义变量报错

# ====================== 配置参数（可根据你的项目修改）======================
MAIN_SCRIPT="main.py"          # 你的主程序脚本
APP_NAME="文件查重"            # 生成的 app 名称
ICON_FILE="tubiao.icns"        # 应用图标文件（需在当前目录）
PYTHON_CMD="python3.10"        # 你的 Python 路径（确保是 3.10，与之前打包一致）
SETUP_FILE="setup.py"          # 之前修改后的 setup.py 文件
# ==========================================================================

# ---------------------- 颜色输出工具函数 ----------------------
echo_green() { echo -e "\033[32m$1\033[0m"; }
echo_red() { echo -e "\033[31m$1\033[0m"; }
echo_yellow() { echo -e "\033[33m$1\033[0m"; }


# ---------------------- 步骤1：检查必要文件是否存在 ----------------------
echo "🔍 【步骤1/5】检查项目文件是否完整..."
if [ ! -f "$MAIN_SCRIPT" ]; then
    echo_red "错误：主脚本 $MAIN_SCRIPT 不存在！请确认脚本路径正确。"
    exit 1
fi
if [ ! -f "$SETUP_FILE" ]; then
    echo_red "错误：打包配置文件 $SETUP_FILE 不存在！请先创建 setup.py（参考之前提供的完整配置）。"
    exit 1
fi
if [ ! -f "$ICON_FILE" ]; then
    echo_yellow "警告：图标文件 $ICON_FILE 不存在！将使用默认图标。"
fi
echo_green "✅ 项目文件检查通过！"


# ---------------------- 步骤2：检查依赖是否安装 ----------------------
echo -e "\n🔍 【步骤2/5】检查必要依赖是否安装..."

# 检查 Python 是否存在
if ! command -v "$PYTHON_CMD" &> /dev/null; then
    echo_red "错误：未找到 $PYTHON_CMD！请先安装 Python 3.10（命令：brew install python@3.10）。"
    exit 1
fi

# 检查 pip 是否可用
PIP_CMD="${PYTHON_CMD} -m pip"
if ! $PYTHON_CMD -m pip --version &> /dev/null; then
    echo_red "错误：$PYTHON_CMD 的 pip 不可用！请修复 Python 环境。"
    exit 1
fi

# 检查关键依赖（py2app、PyQt5、xxhash）
required_packages=("py2app" "PyQt5" "PyQt5-sip" "xxhash")
for pkg in "${required_packages[@]}"; do
    if ! $PIP_CMD show "$pkg" &> /dev/null; then
        echo_yellow "依赖 $pkg 未安装，正在自动安装..."
        $PIP_CMD install --upgrade "$pkg"
    fi
done
echo_green "✅ 所有依赖检查/安装完成！"


# ---------------------- 步骤3：清理旧构建缓存 ----------------------
echo -e "\n🗑️  【步骤3/5】清理旧构建文件（避免缓存干扰）..."
rm -rf build/ dist/ *.egg-info  # 删除 py2app 生成的缓存目录
if [ -d "build" ] || [ -d "dist" ]; then
    echo_red "错误：旧构建文件清理失败！请手动删除 build/ 和 dist/ 目录后重试。"
    exit 1
fi
echo_green "✅ 旧构建文件清理完成！"


# ---------------------- 步骤4：执行 py2app 打包 ----------------------
echo -e "\n🚀 【步骤4/5】开始执行打包（可能需要1-3分钟）..."
$PYTHON_CMD "$SETUP_FILE" py2app

# 检查打包是否生成 app 文件
APP_PATH="dist/${APP_NAME}.app"
if [ ! -d "$APP_PATH" ]; then
    echo_red "错误：打包失败！未生成 $APP_PATH。请查看上方报错信息，重点检查："
    echo_red "1. setup.py 中 PyQt5 插件路径配置是否正确"
    echo_red "2. 主脚本 $MAIN_SCRIPT 是否有语法错误"
    exit 1
fi
echo_green "✅ 打包成功！app 路径：$APP_PATH"


# ---------------------- 步骤5：验证打包结果（关键：检查 Qt 插件） ----------------------
echo -e "\n✅ 【步骤5/5】验证打包完整性..."

# 1. 检查 Qt 平台插件（macOS 必需的 libqcocoa.dylib）
QT_PLUGIN_PATH="${APP_PATH}/Contents/PlugIns/platforms"
if [ ! -d "$QT_PLUGIN_PATH" ]; then
    echo_red "警告：Qt 插件目录 $QT_PLUGIN_PATH 不存在！可能导致 app 崩溃。"
    echo_red "请检查 setup.py 中 QT_PLUGIN_PATH 配置和插件复制逻辑。"
else
    # 检查是否包含 macOS 必需的 cocoa 插件
    if ls "$QT_PLUGIN_PATH"/libqcocoa*.dylib &> /dev/null; then
        echo_green "✅ Qt 平台插件（libqcocoa.dylib）已正确复制！"
    else
        echo_red "警告：Qt 插件目录缺少 libqcocoa.dylib！app 运行会崩溃。"
        echo_red "解决方案：确认 setup.py 中 qt5_root 路径指向 PyQt5/Qt5 目录。"
    fi
fi

# 2. 检查主程序二进制文件
APP_BIN_PATH="${APP_PATH}/Contents/MacOS/${APP_NAME}"
if [ -f "$APP_BIN_PATH" ]; then
    echo_green "✅ 主程序二进制文件存在：$APP_BIN_PATH"
else
    echo_red "错误：主程序二进制文件缺失！打包过程异常。"
    exit 1
fi


# ---------------------- 最终提示 ----------------------
echo -e "\n"
echo_green "🎉 一键打包流程全部完成！"
echo -e "\n📌 后续操作建议："
echo "1. 测试运行 app："
echo "   open $APP_PATH"
echo "2. 若 app 崩溃，查看详细日志："
echo "   $APP_BIN_PATH"
echo "3. 常见问题解决："
echo "   - 崩溃提示 'Qt platform plugin'：检查 setup.py 中 Qt 插件复制逻辑"
echo "   - 缺失 PyQt5 模块：确保 setup.py 的 packages 包含 'PyQt5'"
echo "   - 图标不显示：确认 $ICON_FILE 在当前目录，且 setup.py 中 iconfile 路径正确"