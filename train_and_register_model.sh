#!/usr/bin/env bash

python3 demo_local_runner.py --training_type=batch
python3 agent_controller.py --cmd=decl --zk_servers=zookeeper://zookeeper:2181 --bzid=monolith_serving_test --export_base=/tmp/movie_lens_tutorial/exported_models --overwrite=1 --model_name=movie_lens_tutorial --arch=entry_ps --layout=test
python3 agent_controller.py --cmd=pub --zk_servers=zookeeper://zookeeper:2181 --bzid=monolith_serving_test --export_base=/tmp/movie_lens_tutorial/exported_models --overwrite=1 --model_name=movie_lens_tutorial:* --arch=entry_ps --layout=test