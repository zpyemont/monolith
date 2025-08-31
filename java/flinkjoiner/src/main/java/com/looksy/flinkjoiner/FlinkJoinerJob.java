package com.looksy.flinkjoiner;

import java.time.Duration;
import java.util.Properties;

import org.apache.flink.api.common.eventtime.WatermarkStrategy;
import org.apache.flink.connector.kafka.sink.KafkaRecordSerializationSchema;
import org.apache.flink.connector.kafka.sink.KafkaSink;
import org.apache.flink.connector.kafka.source.KafkaSource;
import org.apache.flink.connector.kafka.source.enumerator.initializer.OffsetsInitializer;
import org.apache.flink.streaming.api.datastream.ConnectedStreams;
import org.apache.flink.streaming.api.datastream.DataStream;
import org.apache.flink.streaming.api.environment.StreamExecutionEnvironment;

import com.looksy.flinkjoiner.proto.ActionEvent;
import com.looksy.flinkjoiner.proto.FeatureEvent;
import com.looksy.flinkjoiner.proto.TrainingSample;

public class FlinkJoinerJob {

    private static final String ACTION_EVENT_TOPIC = "action-event-topic";
    private static final String FEATURE_EVENT_TOPIC = "feature-event-topic";
    private static final String TRAINING_SAMPLE_TOPIC = "training-sample-topic";
    private static final String BOOTSTRAP_SERVERS = "bootstrap.servers";

    public static void main(String[] args) throws Exception {
        final StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        Properties kafkaProperties = new Properties();
        String brokers = System.getenv("KAFKA_BOOTSTRAP_SERVERS");
        if (brokers == null || brokers.isEmpty()) {
            brokers = "my-cluster-kafka-bootstrap.kafka.svc.cluster.local:9092";
        }

        kafkaProperties.setProperty(BOOTSTRAP_SERVERS, "kafka.kafka.svc.cluster.local:9092");
        KafkaSource<ActionEvent> actionConsumer = KafkaSource.<ActionEvent>builder()
                                                            .setBootstrapServers(brokers)
                                                            .setTopics(ACTION_EVENT_TOPIC)
                                                            .setGroupId("my-group")
                                                            .setStartingOffsets(OffsetsInitializer.earliest())
                                                            .setValueOnlyDeserializer(new ActionEventDeserializer())
                                                            .build();

        DataStream<ActionEvent> actions = env.fromSource(actionConsumer, 
        WatermarkStrategy.<ActionEvent>forBoundedOutOfOrderness(Duration.ofSeconds(30))
                        .withTimestampAssigner((event, timestamp) -> event.getEventTime()), "Kafka Source");

        KafkaSource<FeatureEvent> featureConsumer = KafkaSource.<FeatureEvent>builder()
                                                            .setBootstrapServers(brokers)
                                                            .setTopics(FEATURE_EVENT_TOPIC)
                                                            .setGroupId("my-group")
                                                            .setStartingOffsets(OffsetsInitializer.earliest())
                                                            .setValueOnlyDeserializer(new FeatureEventDeserializer())
                                                            .build();

        DataStream<FeatureEvent> features = env.fromSource(featureConsumer,
        WatermarkStrategy.<FeatureEvent>forBoundedOutOfOrderness(Duration.ofSeconds(30))
                        .withTimestampAssigner((event, timestamp) -> event.getEventTime()), "Kafka Source");
        ConnectedStreams<FeatureEvent, ActionEvent> connectedStreams = 
        features.keyBy(event -> event.getRequestId())
                          .connect(actions.keyBy(event -> event.getRequestId()));

        DataStream<TrainingSample> trainingSamples = connectedStreams.process(new JoinCoProcessFunction());

        KafkaSink<TrainingSample> sink = KafkaSink.<TrainingSample>builder()
        .setBootstrapServers(brokers)
        .setRecordSerializer(KafkaRecordSerializationSchema.builder()
            .setTopic(TRAINING_SAMPLE_TOPIC)
            .setValueSerializationSchema(new TrainingSampleSerializer())
            .build()
        )
        // .setDeliveryGuarantee()
        .build();
        trainingSamples.sinkTo(sink);
        env.execute("FlinkJoinerJob");
    }   
}
