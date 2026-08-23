# Copyright (C) DSO-SR-SEP
#
# SPDX-License-Identifier: MIT

"""Small, dependency-free helpers to build the minimal Task/Segment/Job/User graph
this app's tests need, without pulling in CVAT's full task-creation pipeline (media
extractors, RQ jobs, etc.) -- mirrors the pattern used in
`cvat.apps.geospatial.tests.test_services_integration`.
"""

from __future__ import annotations

from django.contrib.auth.models import User

from cvat.apps.engine.models import Data, Job, JobType, Label, Project, Segment, Task


def make_user(username: str) -> User:
    return User.objects.create_user(username=username, password="password")  # noqa: S106


def make_project(*, owner: User | None = None) -> Project:
    return Project.objects.create(name="ml-processing-project", owner=owner)


def make_task(*, project: Project | None = None, owner: User | None = None) -> Task:
    data = Data.objects.create(start_frame=0, stop_frame=4)
    return Task.objects.create(
        name="ml-processing-task", data=data, project=project, owner=owner
    )


def make_label(task: Task, name: str = "car") -> Label:
    return Label.objects.create(task=task, name=name)


def make_job(task: Task, *, assignee: User | None = None) -> Job:
    segment = Segment.objects.create(task=task, start_frame=0, stop_frame=4)
    return Job.objects.create(segment=segment, type=JobType.ANNOTATION, assignee=assignee)
