#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved


import os
import unittest
from copy import deepcopy
from typing import Dict

import pytorch_lightning as pl  # type: ignore
import torch
from d2go.config import CfgNode, temp_defrost
from d2go.runner import create_runner
from d2go.runner.lightning_task import GeneralizedRCNNTask
from d2go.utils.testing import meta_arch_helper as mah
from d2go.utils.testing.helper import tempdir
from detectron2.utils.events import EventStorage
from pytorch_lightning.callbacks.model_checkpoint import ModelCheckpoint
from torch import Tensor


class TestLightningTask(unittest.TestCase):
    def _get_cfg(self, tmp_dir: str) -> CfgNode:
        cfg = mah.create_detection_cfg(GeneralizedRCNNTask, tmp_dir)
        cfg.TEST.EVAL_PERIOD = cfg.SOLVER.MAX_ITER
        return cfg

    def _compare_state_dict(
        self, state1: Dict[str, Tensor], state2: Dict[str, Tensor]
    ) -> bool:
        if state1.keys() != state2.keys():
            return False

        for k in state1:
            if not torch.allclose(state1[k], state2[k]):
                return False
        return True

    @tempdir
    def test_load_from_checkpoint(self, tmp_dir) -> None:
        task = GeneralizedRCNNTask(self._get_cfg(tmp_dir))

        checkpoint_callback = ModelCheckpoint(dirpath=task.cfg.OUTPUT_DIR)
        params = {
            "max_steps": 1,
            "limit_train_batches": 1,
            "num_sanity_val_steps": 0,
            "callbacks": [checkpoint_callback],
        }
        trainer = pl.Trainer(**params)
        with EventStorage() as storage:
            task.storage = storage
            trainer.fit(task)
            ckpt_path = os.path.join(tmp_dir, "test.ckpt")
            trainer.save_checkpoint(ckpt_path)
            self.assertTrue(os.path.exists(ckpt_path))

            # load model weights from checkpoint
            task2 = GeneralizedRCNNTask.load_from_checkpoint(ckpt_path)
            self.assertTrue(
                self._compare_state_dict(
                    task.model.state_dict(), task2.model.state_dict()
                )
            )

    @tempdir
    def test_train_ema(self, tmp_dir):
        cfg = self._get_cfg(tmp_dir)
        cfg.MODEL_EMA.ENABLED = True
        cfg.MODEL_EMA.DECAY = 0.7
        task = GeneralizedRCNNTask(cfg)
        init_state = deepcopy(task.model.state_dict())

        trainer = pl.Trainer(
            max_steps=1,
            limit_train_batches=1,
            num_sanity_val_steps=0,
        )
        with EventStorage() as storage:
            task.storage = storage
            trainer.fit(task)

        for k, v in task.model.state_dict().items():
            init_state[k].copy_(init_state[k] * 0.7 + 0.3 * v)

        self.assertTrue(
            self._compare_state_dict(init_state, task.ema_state.state_dict())
        )

    @tempdir
    def test_load_ema_weights(self, tmp_dir):
        cfg = self._get_cfg(tmp_dir)
        cfg.MODEL_EMA.ENABLED = True
        task = GeneralizedRCNNTask(cfg)
        checkpoint_callback = ModelCheckpoint(
            dirpath=task.cfg.OUTPUT_DIR, save_last=True
        )

        trainer = pl.Trainer(
            max_steps=1,
            limit_train_batches=1,
            num_sanity_val_steps=0,
            callbacks=[checkpoint_callback],
        )

        with EventStorage() as storage:
            task.storage = storage
            trainer.fit(task)

        # load EMA weights from checkpoint
        task2 = GeneralizedRCNNTask.load_from_checkpoint(
            os.path.join(tmp_dir, "last.ckpt")
        )
        self.assertTrue(
            self._compare_state_dict(
                task.ema_state.state_dict(), task2.ema_state.state_dict()
            )
        )

        # apply EMA weights to model
        task2.ema_state.apply_to(task2.model)
        self.assertTrue(
            self._compare_state_dict(
                task.ema_state.state_dict(), task2.model.state_dict()
            )
        )

    def test_create_runner(self):
        task_cls = create_runner(
            f"{GeneralizedRCNNTask.__module__}.{GeneralizedRCNNTask.__name__}"
        )
        self.assertTrue(task_cls == GeneralizedRCNNTask)

    @tempdir
    def test_build_model(self, tmp_dir):
        cfg = self._get_cfg(tmp_dir)
        cfg.MODEL_EMA.ENABLED = True
        task = GeneralizedRCNNTask(cfg)
        checkpoint_callback = ModelCheckpoint(
            dirpath=task.cfg.OUTPUT_DIR, save_last=True
        )

        trainer = pl.Trainer(
            max_steps=1,
            limit_train_batches=1,
            num_sanity_val_steps=0,
            callbacks=[checkpoint_callback],
        )

        with EventStorage() as storage:
            task.storage = storage
            trainer.fit(task)

        # test building untrained model
        model = GeneralizedRCNNTask.build_model(cfg)
        self.assertTrue(model.training)

        # test loading regular weights
        with temp_defrost(cfg):
            cfg.MODEL.WEIGHTS = os.path.join(tmp_dir, "last.ckpt")
            model = GeneralizedRCNNTask.build_model(cfg, eval_only=True)
            self.assertFalse(model.training)
            self.assertTrue(
                self._compare_state_dict(model.state_dict(), task.model.state_dict())
            )

        # test loading EMA weights
        with temp_defrost(cfg):
            cfg.MODEL.WEIGHTS = os.path.join(tmp_dir, "last.ckpt")
            cfg.MODEL_EMA.USE_EMA_WEIGHTS_FOR_EVAL_ONLY = True
            model = GeneralizedRCNNTask.build_model(cfg, eval_only=True)
            self.assertFalse(model.training)
            self.assertTrue(
                self._compare_state_dict(
                    model.state_dict(), task.ema_state.state_dict()
                )
            )
