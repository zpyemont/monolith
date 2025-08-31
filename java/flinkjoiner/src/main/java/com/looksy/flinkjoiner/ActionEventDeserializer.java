package com.looksy.flinkjoiner;

import java.io.IOException;

import org.apache.flink.api.common.serialization.DeserializationSchema;
import org.apache.flink.api.common.typeinfo.TypeInformation;

import com.looksy.flinkjoiner.proto.ActionEvent;

public class ActionEventDeserializer implements DeserializationSchema<ActionEvent>{

    public ActionEventDeserializer() {
    }

    @Override
    public ActionEvent deserialize(byte[] message) throws IOException {
        try {
            return ActionEvent.parseFrom(message);
        } catch (Exception e) {
            throw new IOException("Failed to deserialize ActionEvent", e);
        }
    }

    @Override
    public boolean isEndOfStream(ActionEvent nextElement) {
        return false;
    }

    @Override
    public TypeInformation<ActionEvent> getProducedType() {
        return TypeInformation.of(ActionEvent.class);
    }
    
}
