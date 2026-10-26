"""PyInstaller和源码启动使用同一公开入口。"""

if __name__ == "__main__":
    from multiprocessing import freeze_support

    # 必须在加载Qt及解析应用参数之前分流Windows打包进程的子进程入口。
    freeze_support()
    from cqu_monitor.__main__ import main

    raise SystemExit(main())
