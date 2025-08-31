package com.looksy.flinkjoiner;

import org.apache.flink.api.common.serialization.SerializationSchema;

import com.looksy.flinkjoiner.proto.TrainingSample;

public class TrainingSampleSerializer implements SerializationSchema<TrainingSample>{

    @Override
    public byte[] serialize(TrainingSample element) {
        return element.toByteArray();
    }
    
}
