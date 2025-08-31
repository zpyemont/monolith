package com.looksy.flinkjoiner;
import com.looksy.flinkjoiner.proto.ActionEvent;
import com.looksy.flinkjoiner.proto.FeatureEvent;

public class CreatTestCases {
    public static void main (String[] args) {
        ActionEvent actionEvent = ActionEvent.newBuilder()
                .setRequestId(123L)
                .setEventTime((long)System.currentTimeMillis())
                .setActionData("test action")
                .build();
        System.out.println("ActionEvent: " + new String(actionEvent.toByteArray()));
        FeatureEvent featureEvent = FeatureEvent.newBuilder().setRequestId(345L).setEventTime((long)System.currentTimeMillis()).setFeatureData("test feature").build();
        System.out.println("FeatureEvent: " + new String(featureEvent.toByteArray()));

    }
}
