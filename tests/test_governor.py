"""Tests for resource governor and job queue."""

import time
from glimpse.governor import ResourceGovernor, GovernorMode, GovernorDecision
from glimpse.queue import JobQueue, JobPriority, IndexJob
from glimpse.config import HardwareProfile


class TestResourceGovernor:
    def test_max_effort_mode(self):
        gov = ResourceGovernor(HardwareProfile.BALANCED, max_effort=True)
        assert gov.mode == GovernorMode.MAX_EFFORT

        d = gov.poll()
        assert d.should_run is True
        assert d.batch_size > 0
        assert d.sleep_ms == 0
        assert d.reason == "max_effort"

    def test_governed_mode_pauses_on_high_cpu(self):
        from glimpse.governor import ProfileDefaults
        gov = ResourceGovernor(HardwareProfile.BALANCED, max_effort=False)
        # Mock high CPU by replacing profile with low pause threshold
        gov._profile = ProfileDefaults(
            worker_threads=2,
            cpu_high_pct=70.0,
            cpu_pause_pct=0.1,  # will always trigger
            batch_size=24,
            idle_gate_min=2.0,
            battery_pause_below_pct=25,
            video_default_on=False,
            larger_models=False,
        )
        d = gov.poll()
        # On Windows, cpu_percent may be >0.1, so should pause
        assert d.should_run is False or d.batch_size == 0

    def test_mode_switch(self):
        gov = ResourceGovernor(HardwareProfile.BALANCED, max_effort=True)
        assert gov.mode == GovernorMode.MAX_EFFORT
        gov.set_mode(GovernorMode.GOVERNED)
        assert gov.mode == GovernorMode.GOVERNED
        gov.set_mode(GovernorMode.MAX_EFFORT)
        assert gov.mode == GovernorMode.MAX_EFFORT

    def test_profile_switch(self):
        gov = ResourceGovernor(HardwareProfile.BALANCED, max_effort=True)
        assert gov._profile.worker_threads == 2
        gov.set_profile(HardwareProfile.MINIMAL)
        assert gov._profile.worker_threads == 1
        gov.set_profile(HardwareProfile.PERFORMANCE)
        assert gov._profile.worker_threads == 4


class TestJobQueue:
    def setup_method(self):
        self.gov = ResourceGovernor(HardwareProfile.BALANCED, max_effort=True)
        self.queue = JobQueue(self.gov)

    def test_enqueue_dequeue(self):
        self.queue.enqueue(1, "/path/file1.txt", JobPriority.NORMAL)
        self.queue.enqueue(2, "/path/file2.txt", JobPriority.HIGH)

        job = self.queue.dequeue()
        assert job is not None
        assert job.file_id == 2  # HIGH priority first
        assert job.path == "/path/file2.txt"

        job = self.queue.dequeue()
        assert job.file_id == 1

    def test_deduplication(self):
        self.queue.enqueue(1, "/path/file1.txt", JobPriority.NORMAL)
        self.queue.enqueue(1, "/path/file1.txt", JobPriority.HIGH)  # duplicate
        assert self.queue.depth() == 1

    def test_pause_resume(self):
        self.queue.enqueue(1, "/path/file1.txt")
        self.queue.pause()
        # Should not dequeue when paused (short timeout)
        job = self.queue.dequeue(timeout=1.0)
        assert job is None
        self.queue.resume()
        job = self.queue.dequeue(timeout=2.0)
        assert job is not None

    def test_max_effort_toggle(self):
        self.queue.set_max_effort(True)
        assert self.gov.mode == GovernorMode.MAX_EFFORT
        self.queue.set_max_effort(False)
        assert self.gov.mode == GovernorMode.GOVERNED

    def test_stats(self):
        self.queue.enqueue(1, "/a.txt")
        self.queue.enqueue(2, "/b.txt")
        stats = self.queue.stats()
        assert stats["pending"] == 2
        assert stats["in_flight"] == 0

        job = self.queue.dequeue()
        stats = self.queue.stats()
        assert stats["pending"] == 1
        assert stats["in_flight"] == 1

        self.queue.complete(job.file_id, True)
        stats = self.queue.stats()
        assert stats["pending"] == 1
        assert stats["in_flight"] == 0
        assert stats["total_completed"] == 1

    def test_requeue(self):
        self.queue.enqueue(1, "/a.txt", JobPriority.NORMAL)
        job = self.queue.dequeue()
        # Simulate failure
        self.queue.complete(job.file_id, False)
        # Requeue with higher priority
        self.queue.requeue(job.file_id, job.path, JobPriority.HIGH)
        job2 = self.queue.dequeue()
        assert job2.priority == JobPriority.HIGH


class TestIntegration:
    def test_worker_processes_batch(self):
        gov = ResourceGovernor(HardwareProfile.BALANCED, max_effort=True)
        queue = JobQueue(gov)

        processed = []

        def worker(job):
            processed.append(job.file_id)
            return True

        for i in range(5):
            queue.enqueue(i, f"/path/file{i}.txt")

        count = queue.process_batch(worker, batch_size=3)
        assert count == 3
        assert len(processed) == 3

        count = queue.process_batch(worker, batch_size=3)
        assert count == 2
        assert len(processed) == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])