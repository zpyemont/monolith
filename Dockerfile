FROM hanzhi713/monolith:ubuntu22.04

# Copy necessary scripts and configs
COPY ./movie_online_model.py /movie_online_model.py
COPY markdown/demo/ml_dataset.py /ml_dataset.py
COPY markdown/demo/kafka_receiver.py /kafka_receiver.py
# Optionally remove or keep demo_local_runner.py if not needed
# COPY markdown/demo/demo_local_runner.py /demo_local_runner.py
COPY demo.conf /demo.conf
COPY monolith/agent_service/agent_controller.py /usr/local/lib/python3.8/site-packages/monolith/agent_service/agent_controller.py
COPY train_and_register_model.sh /train_and_register_model.sh
COPY conf/platform_config_file.cfg /conf/platform_config_file.cfg

RUN ln -s /usr/local/lib/python3.8/site-packages/monolith/agent_service/agent.py /agent.py

# Install TensorFlow I/O for BigQuery Storage API ingestion (compatible with TF 2.4.x)
RUN python3 -m pip install --no-cache-dir \
    tensorflow-io==0.17.0 \
    google-cloud-bigquery \
    google-cloud-bigquery-storage

# TF Serving: copy necessary files from extracted artifacts
COPY extracted_artifacts /output

ENV MONOLITH_TFS_BINARY=/output/bin/tensorflow_model_server

# Prepare checkpoint directory (if using warm-start)
RUN mkdir -p /checkpoints && chmod 777 /checkpoints

COPY ./monolith/native_training/zk_utils.py /usr/local/lib/python3.8/site-packages/monolith/native_training/zk_utils.py
COPY ./monolith/agent_service/tfs_wrapper.py /usr/local/lib/python3.8/site-packages/monolith/agent_service/tfs_wrapper.py

ENTRYPOINT ["python3", "/movie_online_model.py"]
# Default to batch training mode; adjust CMD as needed for online training
CMD ["--training_type=batch"]
