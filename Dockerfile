FROM hanzhi713/monolith:ubuntu22.04

# Java will be mounted
ENV JAVA_HOME=/opt/jdk/jdk1.8.0_201
ENV PATH=$JAVA_HOME/bin:$PATH

# Hadoop will be mounted
ENV HADOOP_HOME=/usr/lib/hadoop
ENV HADOOP_CONF_DIR=$HADOOP_HOME/etc/hadoop
ENV PATH=$HADOOP_HOME/bin:$PATH

# Copy necessary scripts and configs
COPY markdown/demo/movie_online_model.py /movie_online_model.py
COPY markdown/demo/ml_dataset.py /ml_dataset.py
COPY markdown/demo/kafka_receiver.py /kafka_receiver.py
# Optionally remove or keep demo_local_runner.py if not needed
# COPY markdown/demo/demo_local_runner.py /demo_local_runner.py
COPY demo.conf /demo.conf
COPY monolith/agent_service/agent_controller.py /agent_controller.py
COPY train_and_register_model.sh /train_and_register_model.sh
COPY conf/platform_config_file.cfg /conf/platform_config_file.cfg

RUN ln -s /usr/local/lib/python3.8/site-packages/monolith/agent_service/agent.py /agent.py

# Install Java
RUN wget -q https://github.com/frekele/oracle-java/releases/download/8u201-b09/jdk-8u201-linux-x64.tar.gz && \
    mkdir /opt/jdk && \
    tar -xzf jdk-8u201-linux-x64.tar.gz -C /opt/jdk && \
    update-alternatives --install /usr/bin/java java /opt/jdk/jdk1.8.0_201/bin/java 100 && \
    update-alternatives --install /usr/bin/javac javac /opt/jdk/jdk1.8.0_201/bin/javac 100

# Install Hadoop
RUN wget -q https://dlcdn.apache.org/hadoop/common/hadoop-3.3.6/hadoop-3.3.6.tar.gz && \
    tar -xzf hadoop-3.3.6.tar.gz && mv hadoop-3.3.6 /usr/lib/hadoop && rm hadoop-3.3.6.tar.gz

# TF Serving: copy necessary files
COPY output /output

ENV MONOLITH_TFS_BINARY=/output/tensorflow_model_server.runfiles/__main__/external/org_tensorflow_serving/tensorflow_serving/model_servers/tensorflow_model_server

# Prepare checkpoint directory (if using warm-start)
RUN mkdir -p /checkpoints && chmod 777 /checkpoints

# Production ENTRYPOINT: run demo_model.py
ENTRYPOINT ["python3", "/movie_online_model.py"]
# Default to batch training mode; adjust CMD as needed for online training
CMD ["--training_type=batch"]
