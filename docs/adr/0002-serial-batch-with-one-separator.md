# 批次串行执行，整批复用一个已加载的分离模型

批次按队列顺序逐首转换，同一时刻只有一首在跑；整个批次只构造一次 `Separator` 并复用，而不是每首重建。

**理由**：`htdemucs_ft` 是四个子模型的集合（`.venv/Lib/site-packages/demucs/remote/htdemucs_ft.yaml`），每次构造 `Separator` 都要把这四个模型的权重重新加载一遍（`Separator.__init__` 里的 `_load_model()`，`.venv/Lib/site-packages/demucs/api.py:119`），在 CPU 上是每首歌都要等的固定开销。`Separator` 只持有权重与可调参数，不持有单次调用的中间状态，官方实现自己也是一次构造、循环处理多个文件（`api.py:353-367`）。demucs 内部没有任何 `torch.cuda.empty_cache()`，显存随实例存活而保留，因此「每首新建」既慢又会制造显存抖动。

**为什么不能并行**：`apply_model` 的输出缓冲按整首歌长度一次分配（`.venv/Lib/site-packages/demucs/apply.py:259` 的 `th.zeros(batch, len(model.sources), channels, length, ...)`），显存峰值正比于曲长与并行数之积。本机是 7.93 GB 显存的 RTX 5060 Laptop，两首并行就有实质的 OOM 风险。CPU 路径同样会互相抢占，二十首歌并行只会让每首都变慢。

**取消**：demucs 唯一的中断方式是让 callback 抛 `KeyboardInterrupt`（`.venv/Lib/site-packages/demucs/api.py:106`），所以必须把 callback 透传进去，中断粒度是一个分段（`segment`）的长度。中断之后 `Separator` 实例仍可继续用于后续曲目与后续批次。

**改判记录**：拷问阶段曾有一轮明确选过「两个曲目并行分离」，下一轮又认可了一份写着「不并行」的 ADR 措辞——而后者是在把上一轮记错成「选了串行」的前提下问出来的。最终取串行，依据是硬件约束与后一次的明确选择。这里把改判写下来，而不是假装从来没提过并行；如果哪天换到大显存机器、用户又真的需要吞吐，这份 ADR 就是需要重新审视的第一份文档。
