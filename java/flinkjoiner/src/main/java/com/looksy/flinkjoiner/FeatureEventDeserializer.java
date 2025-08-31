package com.looksy.flinkjoiner;
import java.io.IOException;

import org.apache.flink.api.common.serialization.DeserializationSchema;
import org.apache.flink.api.common.typeinfo.TypeInformation;

import com.looksy.flinkjoiner.proto.FeatureEvent;

public class FeatureEventDeserializer implements DeserializationSchema<FeatureEvent>{

    @Override
    public FeatureEvent deserialize(byte[] message) throws IOException {
        try {
            return FeatureEvent.parseFrom(message);
        } catch (Exception e) {
            throw new IOException("Failed to deserialize FeatureEvent", e);
        }
    }

    @Override
    public boolean isEndOfStream(FeatureEvent nextElement) {
        return false;
    }

    @Override
    public TypeInformation<FeatureEvent> getProducedType() {
        return TypeInformation.of(FeatureEvent.class);
    }

    
}
