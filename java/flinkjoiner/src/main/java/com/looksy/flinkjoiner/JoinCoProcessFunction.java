package com.looksy.flinkjoiner;

import com.looksy.flinkjoiner.proto.TrainingSample;
import com.looksy.flinkjoiner.proto.ActionEvent;
import com.looksy.flinkjoiner.proto.FeatureEvent;

import org.apache.flink.api.common.state.ValueState;
import org.apache.flink.api.common.state.ValueStateDescriptor;
import org.apache.flink.api.common.typeinfo.TypeHint;
import org.apache.flink.api.common.typeinfo.TypeInformation;
import org.apache.flink.configuration.Configuration;
import org.apache.flink.streaming.api.functions.co.KeyedCoProcessFunction;
import org.apache.flink.util.Collector;

public class JoinCoProcessFunction
    extends KeyedCoProcessFunction<String, FeatureEvent, ActionEvent, TrainingSample> {

    private static final long TIMEOUT = 3_600_000L; // 1h

    // keyed, per‑requestId state
    private transient ValueState<FeatureEvent> featureState;
    private transient ValueState<ActionEvent>   actionState;


    public void open(Configuration parameters) throws Exception {
        // You must supply TypeInformation explicitly in 2.0.0
        TypeInformation<FeatureEvent> featureTI =
            TypeInformation.of(new TypeHint<FeatureEvent>() {});
        ValueStateDescriptor<FeatureEvent> featureDesc =
            new ValueStateDescriptor<>("featureState", featureTI);
        featureState = getRuntimeContext().getState(featureDesc);

        TypeInformation<ActionEvent> actionTI =
            TypeInformation.of(new TypeHint<ActionEvent>() {});
        ValueStateDescriptor<ActionEvent> actionDesc =
            new ValueStateDescriptor<>("actionState", actionTI);
        actionState = getRuntimeContext().getState(actionDesc);
    }

    @Override
    public void processElement1(
            FeatureEvent feature,
            Context ctx,
            Collector<TrainingSample> out) throws Exception {

        ActionEvent action = actionState.value();
        if (action != null) {
            // both sides present → emit and clear
            out.collect(TrainingSample.newBuilder()
                .setRequestId(feature.getRequestId())
                .setFeatureData(feature.getFeatureData())
                .setActionData(action.getActionData())
                .setFeatureEventTime(feature.getEventTime())
                .setActionEventTime(action.getEventTime())
                .build());
            actionState.clear();
        } else {
            // hold and register a cleanup timer
            featureState.update(feature);
            ctx.timerService()
               .registerEventTimeTimer(feature.getEventTime() + TIMEOUT);
        }
    }

    @Override
    public void processElement2(
            ActionEvent action,
            Context ctx,
            Collector<TrainingSample> out) throws Exception {

        FeatureEvent feature = featureState.value();
        if (feature != null) {
            out.collect(TrainingSample.newBuilder()
                .setRequestId(feature.getRequestId())
                .setFeatureData(feature.getFeatureData())
                .setActionData(action.getActionData())
                .setFeatureEventTime(feature.getEventTime())
                .setActionEventTime(action.getEventTime())
                .build());
            featureState.clear();
        } else {
            actionState.update(action);
            ctx.timerService()
               .registerEventTimeTimer(action.getEventTime() + TIMEOUT);
        }
    }

    @Override
    public void onTimer(
            long timestamp,
            OnTimerContext ctx,
            Collector<TrainingSample> out) throws Exception {
        // timer fires → clear any stale state
        featureState.clear();
        actionState.clear();
    }
}

