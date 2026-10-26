"""按完整时间序列评估20分钟夹具内存趋势，保留预热及所有原始点。"""

import argparse
from pathlib import Path
import csv
import html
import json
import statistics

parser = argparse.ArgumentParser(description="分析实际20分钟1Hz夹具的全部内存记录")
parser.add_argument("--output", type=Path, required=True, help="run_memory_trial.py生成的目录")
args = parser.parse_args()
output = args.output.resolve()
report = json.loads((output / "run-report.json").read_text(encoding="utf-8"))
rows = [
    json.loads(line)
    for line in (output / "memory_samples.jsonl").read_text(encoding="utf-8").splitlines()
]
acquisition = [r for r in rows if r["phase"] == "acquisition"]
end = max(r["acquisition_seconds"] for r in acquisition)
stable = [r for r in acquisition if 300 <= r["acquisition_seconds"] <= end]
assert end >= 1200 and len(stable) >= 100, "必须完成约定的实际20分钟运行"


def slope(points, key):
    x = [r["acquisition_seconds"] / 60 for r in points]
    y = [r[key] / 1048576 for r in points]
    avg_x, avg_y = statistics.mean(x), statistics.mean(y)
    return sum((a - avg_x) * (b - avg_y) for a, b in zip(x, y)) / sum((a - avg_x) ** 2 for a in x)


stats = {}
for key in ("private_bytes", "total_private_bytes", "working_set_bytes", "total_working_set_bytes"):
    values = [r[key] / 1048576 for r in stable]
    previous = [
        r[key] / 1048576 for r in stable if end - 600 <= r["acquisition_seconds"] < end - 300
    ]
    final = [r[key] / 1048576 for r in stable if end - 300 <= r["acquisition_seconds"] <= end]
    stats[key] = dict(
        min_mib=min(values),
        max_mib=max(values),
        median_mib=statistics.median(values),
        last5min_median_mib=statistics.median(final),
        previous5min_median_mib=statistics.median(previous),
        median_delta_mib=statistics.median(final) - statistics.median(previous),
        slope_mib_per_min=slope(stable, key),
    )
checks = dict(
    complete_20min_trial=report["passed"],
    main_commit_last5min_delta_at_most_5mib=stats["private_bytes"]["median_delta_mib"] <= 5,
    all_process_commit_last5min_delta_at_most_8mib=stats["total_private_bytes"]["median_delta_mib"]
    <= 8,
    main_commit_slope_at_most_1mib_per_min=stats["private_bytes"]["slope_mib_per_min"] <= 1,
    all_process_commit_slope_at_most_1mib_per_min=stats["total_private_bytes"]["slope_mib_per_min"]
    <= 1,
    chart_buffers_stay_2000=all(r["points_min"] == r["points_max"] == 2000 for r in stable),
    controller_histories_stay_150=all(
        all(n == 150 for n in r["controller_history"].values()) for r in stable
    ),
    no_unrecorded_payload=all(r["unrecorded"] == 0 for r in stable),
    qobject_span_at_most_2=max(r["qobjects"] for r in stable) - min(r["qobjects"] for r in stable)
    <= 2,
)
analysis = dict(
    version=report["version"],
    passed=all(checks.values()),
    checks=checks,
    acquisition_seconds=report["first_session"]["acquisition_seconds"],
    total_test_seconds=report["total_test_seconds"],
    sample_count=len(rows),
    stable_sample_count=len(stable),
    stable_interval_seconds=[300, end],
    stats=stats,
    qobject_range=[min(r["qobjects"] for r in stable), max(r["qobjects"] for r in stable)],
    allocated_block_range=[
        min(r["allocated_blocks"] for r in stable),
        max(r["allocated_blocks"] for r in stable),
    ],
    queue_max=max(r["queue"] for r in acquisition),
    recorder_pending_max=max(r["recorder_pending"] for r in acquisition),
    pending_voltage_max=max(r["pending_voltage"] for r in acquisition),
    absolute_memory_limit_given_by_user=False,
    criteria_note="趋势判据为本轮工程检查容差，并非用户指定MiB上限。预热5分钟，完整保留全部点；工作集受操作系统换页影响。",
    real_hardware_tested=False,
    physical_24h_tested=False,
)
(output / "analysis.json").write_text(
    json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
)
keys = [
    "wall_seconds",
    "acquisition_seconds",
    "phase",
    "label",
    "private_bytes",
    "working_set_bytes",
    "total_private_bytes",
    "total_working_set_bytes",
    "total_handles",
    "qobjects",
    "python_objects",
    "allocated_blocks",
    "python_threads",
    "points_min",
    "points_max",
    "log_blocks",
    "queue",
    "recorder_pending",
    "pending_voltage",
    "unrecorded",
]
with (output / "memory_samples.csv").open("w", encoding="utf-8-sig", newline="") as stream:
    writer = csv.DictWriter(stream, fieldnames=keys, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)


def chart(title, series):
    points = [r for r in rows if r["phase"] in ("acquisition", "pressure_and_acquisition")]
    all_values = [r[key] / 1048576 for key, _ in series for r in points]
    lo, hi = min(all_values), max(all_values)
    pad = max(1, (hi - lo) * 0.12)
    lo, hi = max(0, lo - pad), hi + pad
    width, height = 940, 280

    def x(row):
        return 60 + (width - 90) * min(row["acquisition_seconds"] / end, 1)

    def y(value):
        return height - 40 - (height - 70) * (value - lo) / (hi - lo)

    items = [
        f'<h2>{html.escape(title)}</h2><svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">'
    ]
    items.append('<rect width="100%" height="100%" fill="#fff"/>')
    for i in range(5):
        v = lo + (hi - lo) * i / 4
        yy = y(v)
        items.append(
            f'<line x1="60" y1="{yy:.2f}" x2="910" y2="{yy:.2f}" stroke="#e0e7ec"/><text x="55" y="{yy + 4:.2f}" text-anchor="end" font-size="11">{v:.1f}</text>'
        )
    for minute in (0, 5, 10, 15, 20):
        xx = 60 + 850 * minute / 20
        items.append(
            f'<text x="{xx}" y="265" text-anchor="middle" font-size="12">{minute}分</text>'
        )
    for index, (key, label) in enumerate(series):
        color = ("#098c82", "#d49935")[index]
        path = " ".join(f"{x(r):.2f},{y(r[key] / 1048576):.2f}" for r in points)
        items.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="1.8"/>')
        items.append(
            f'<text x="{100 + index * 290}" y="17" fill="{color}" font-size="12">{html.escape(label)}</text>'
        )
    items.append("</svg>")
    return "".join(items)


labels = {
    "private_bytes": "主进程专用提交量",
    "total_private_bytes": "主进程及5个采集子进程提交量",
    "working_set_bytes": "主进程工作集",
    "total_working_set_bytes": "全部进程工作集合计",
}
table = "".join(
    f"<tr><td>{labels[key]}</td><td>{value['median_mib']:.2f}</td><td>{value['min_mib']:.2f}–{value['max_mib']:.2f}</td><td>{value['median_delta_mib']:+.3f}</td><td>{value['slope_mib_per_min']:+.4f}</td></tr>"
    for key, value in stats.items()
)
conclusion = (
    "在本轮20分钟测试及满缓存压力检查中未见持续增长。"
    if analysis["passed"]
    else "本轮检查未满足全部趋势判据，需继续调查。"
)
page = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>{report["version"]}内存与功能试运行</title><style>body{{max-width:1080px;margin:36px auto;padding:0 22px;font:16px/1.8 Microsoft YaHei,sans-serif;background:#f6f9fc;color:#25394e}}article{{background:white;padding:28px;border:1px solid #dae4ec;border-radius:10px}}h1,h2{{color:#125b63}}h1{{font-size:26px}}h2{{font-size:20px}}table{{border-collapse:collapse;width:100%;font-size:14px}}td,th{{border:1px solid #d8e2e8;padding:8px;text-align:left}}svg{{width:100%;height:auto}}code{{overflow-wrap:anywhere}}</style><article>
<h1>CQU-Monitor {report["version"]} 内存与功能试运行</h1><p>{conclusion}已修复反复打开参数窗口保留控件的问题。测试使用寄存器夹具，禁止真实串口，正式数据未混入测试记录。</p>
<p>实际采集 {analysis["acquisition_seconds"]:.2f} 秒，1Hz；另包含缓存预填、停止、TXT/xlsx导出和重新开始。全程 {analysis["total_test_seconds"]:.2f} 秒。两组控制器状态约每0.2秒读取一次，采集在5个真实隔离子进程中执行。正式应用不包含此夹具。</p>
<table><tr><th>指标（MiB）</th><th>第5–20分钟中位数</th><th>最小–最大</th><th>最后5分钟与前5分钟中位差</th><th>趋势 MiB/分钟</th></tr>{table}</table>
<p>MiB=1048576字节。工作集表示当前驻留物理内存，受操作系统回收/换页影响；专用提交量表示进程的私有提交，不能直接等同为物理内存。工作集合计可能重复计算共享页。两类指标均完整报告。</p>
{chart("专用提交量（MiB）", [("private_bytes", "主进程"), ("total_private_bytes", "主进程及采集子进程合计")])}
{chart("工作集（MiB）", [("working_set_bytes", "主进程"), ("total_working_set_bytes", "主进程及采集子进程合计")])}
<h2>对象、缓存和报警</h2><p>预热后主窗口QObject范围 {analysis["qobject_range"]}；33图始终每图2000点，控制器历史每组150条，日志最多300块。主队列峰值 {analysis["queue_max"]}，记录器待写峰值 {analysis["recorder_pending_max"]}，待定电压峰值 {analysis["pending_voltage_max"]}；停止时队列及待定结果全部排空，读取进程退出。测试确认30室均取得有效样本，所有有效请求完整落入[2,4)秒。</p>
<p>正常输出/等待/换路期间无意外报警；注入的电流超限、掉线和左组倒计时冻结均触发对应报警，确认按钮停止提示音定时器。持续试运行中的声音采用计数替身；另有使用安装目录WAV和真实Windows PlaySound的独立验证，不能据此声称已听到物理扬声器。</p>
<h2>此前1.2.6生命周期专项</h2><p>以下是1.2.6原始专项证据；当前版本没有更改窗口释放或缓存算法。修复前设置窗口打开30次保留30份控件，提交量增加172.148MiB。修复后取消、确认、关闭各100次均无残留，最后50次提交量完全不变。另完成历史200次、放大图60次、实验重开30次、正常导出40次、失败导出30次、清空成功/失败12次、100个无设备子进程退出验证。33图各追加6000点，满缓存后数量和内存稳定。详细脚本与原始记录见验收证据。</p>
<h2>判据与范围</h2><p>{html.escape(analysis["criteria_note"])}用户未指定绝对MiB上限，本次依据趋势、对象释放及缓存上限评估“不会持续增大”；没有调整现有缓存大小或丢弃应保存记录来降低内存。</p><p>本轮不能替代真实RS485设备、干净目标Windows系统及24小时实测。本项使用程序内部的真实Qt按钮信号与声音计数替身；安装版鼠标交互和物理扬声器可闻性仍须现场确认。</p>
<p>原始JSONL、CSV、检查点和分析JSON随“内存检查记录”提供；交付保留可复核记录和重跑脚本，不包含测试数据库或用户实测数据库。</p></article></html>"""
(output / "内存检查报告.html").write_text(page, encoding="utf-8")
print(json.dumps(dict(passed=analysis["passed"], stats=stats, checks=checks), ensure_ascii=False))
